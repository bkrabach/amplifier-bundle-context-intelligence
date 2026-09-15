"""Focused tests for durable native capture recovery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from amplifier_module_hook_context_intelligence.config_resolver import Destination

from amplifier_module_tool_context_intelligence_recover import cli, recovery
from amplifier_module_tool_context_intelligence_recover.recovery import (
    DestinationConfig,
    RecoveryRunner,
    discover_native_artifacts,
    resolve_scan_roots,
)


def _capture(
    root: Path,
    session_id: str = "session-1",
    *,
    working_dir: str = "/workspace/app",
    status: str = "completed",
    ended_at: str = "2026-09-15T01:00:00Z",
    body: bytes | None = None,
) -> Path:
    capture = root / "project" / "sessions" / session_id / "context-intelligence"
    capture.mkdir(parents=True)
    metadata = {
        "format": "context-intelligence",
        "version": "1.0.0",
        "session_id": session_id,
        "workspace": "project",
        "parent_id": "",
        "working_dir": working_dir,
        "status": status,
        "ended_at": ended_at,
    }
    (capture / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    event = {
        "event": "session:start",
        "workspace": "project",
        "data": {"session_id": session_id, "timestamp": "2026-09-15T00:00:00Z"},
    }
    (capture / "events.jsonl").write_bytes(
        body if body is not None else json.dumps(event).encode() + b"\n"
    )
    return capture / "events.jsonl"


def _config(*destinations: Destination) -> DestinationConfig:
    return DestinationConfig(
        destinations={destination.name: destination for destination in destinations},
        invalid_names=(),
        base_path=Path("/not-used"),
    )


@pytest.fixture
def configured_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(
        recovery,
        "resolve_scan_roots",
        lambda _config, explicit: [Path(path).absolute() for path in explicit],
    )
    default = Destination("default", "https://default.example", "default-key", ("**",), ())
    monkeypatch.setattr(recovery, "load_destination_config", lambda _path: _config(default))
    monkeypatch.setattr(
        recovery,
        "load_effective_destination_config",
        lambda _working_dir, settings_path: (recovery.load_destination_config(settings_path), None),
    )


def test_discovery_is_file_led_and_inventory_keeps_bad_metadata(
    tmp_path: Path, configured_roots: None
) -> None:
    good = _capture(tmp_path, "good")
    missing = _capture(tmp_path, "missing")
    (missing.parent / "metadata.json").unlink()
    corrupt = _capture(tmp_path, "corrupt")
    (corrupt.parent / "metadata.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "lookalike" / "events.jsonl").parent.mkdir(parents=True)
    (tmp_path / "lookalike" / "events.jsonl").write_text("{}\n", encoding="utf-8")

    assert discover_native_artifacts([tmp_path]) == [corrupt, good, missing]

    runner = RecoveryRunner(state_dir=tmp_path / "state", job_id="inventory")
    try:
        summary = runner.run([tmp_path], dry_run=True)
    finally:
        runner.close()

    assert summary.inventory == {"deferred": 1, "eligible": 1, "quarantined": 1}
    assert summary.pending is True


def test_routing_fans_out_with_hook_include_exclude_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots: None
) -> None:
    _capture(tmp_path, working_dir="/workspace/team/app")
    team = Destination("team", "https://team.example", "team-key", ("**/team/",), ())
    personal = Destination(
        "personal", "https://personal.example", "personal-key", ("**",), ("**/team/",)
    )
    monkeypatch.setattr(recovery, "load_destination_config", lambda _path: _config(team, personal))

    runner = RecoveryRunner(state_dir=tmp_path / "state", job_id="fanout")
    try:
        summary = runner.run([tmp_path], dry_run=True)
        states = runner.checkpoint.connection.execute(
            "SELECT destination, state FROM delivery ORDER BY destination"
        ).fetchall()
    finally:
        runner.close()

    assert summary.inventory == {"eligible": 1}
    assert [(row["destination"].split(":", 1)[0], row["state"]) for row in states] == [
        ("personal", "filtered"),
        ("team", "pending"),
    ]


def test_project_local_exclude_overrides_global_include_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "owner"
    workspace = home / "workspaces" / "private"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(recovery.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(recovery, "DEFAULT_BASE_PATH", home / "empty-default")
    monkeypatch.setattr(
        recovery, "load_destination_config", lambda _path: DestinationConfig({}, (), home)
    )
    capture = _capture(home / "captures", working_dir=str(workspace))
    global_settings = home / ".amplifier" / "settings.yaml"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text(
        """
overrides:
  hook-context-intelligence:
    config:
      destinations:
        primary:
          url: https://primary.example
          api_key: test-key
          include: ["**"]
""",
        encoding="utf-8",
    )
    local_settings = workspace / ".amplifier" / "settings.yaml"
    local_settings.parent.mkdir()
    local_settings.write_text(
        """
overrides:
  hook-context-intelligence:
    config:
      destinations:
        primary:
          exclude: ["**/private/"]
""",
        encoding="utf-8",
    )
    requests: list[str] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request.url.path) or httpx.Response(500, json={"unexpected": True})
            )
        )
    )
    runner = RecoveryRunner(
        state_dir=home / "state",
        job_id="local-override",
        settings_path=global_settings,
        client=client,
    )
    try:
        summary = runner.run([capture.parent.parent.parent.parent])
    finally:
        runner.close()

    assert summary.inventory == {"filtered": 1}
    assert summary.pending is False
    assert requests == []


def test_unavailable_recorded_working_dir_is_quarantined(
    tmp_path: Path, configured_roots: None
) -> None:
    event = (
        json.dumps(
            {"event": "session:start", "workspace": "project", "data": {"session_id": "no-dir"}}
        ).encode()
        + b"\n"
    )
    _capture(tmp_path, "no-dir", working_dir="", body=event)

    runner = RecoveryRunner(state_dir=tmp_path / "state", job_id="routing")
    try:
        summary = runner.run([tmp_path], dry_run=True)
    finally:
        runner.close()

    assert summary.inventory == {"quarantined": 1}


def test_unavailable_recorded_project_config_blocks_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "owner"
    monkeypatch.setattr(recovery.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(recovery, "DEFAULT_BASE_PATH", home / "empty-default")
    monkeypatch.setattr(
        recovery, "load_destination_config", lambda _path: DestinationConfig({}, (), home)
    )
    _capture(home, working_dir=str(home / "missing-project"))

    runner = RecoveryRunner(state_dir=home / "state", job_id="unavailable-project")
    try:
        summary = runner.run([home], dry_run=True)
    finally:
        runner.close()

    assert summary.inventory == {"blocked": 1}
    assert summary.pending is True


def test_cancelled_native_session_is_eligible(tmp_path: Path, configured_roots: None) -> None:
    _capture(tmp_path, status="cancelled")

    runner = RecoveryRunner(state_dir=tmp_path / "state", job_id="cancelled")
    try:
        summary = runner.run([tmp_path], dry_run=True)
    finally:
        runner.close()

    assert summary.inventory == {"eligible": 1}
    assert summary.pending is True


def test_symlink_escape_is_kept_as_a_lexical_candidate_and_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "owner"
    monkeypatch.setattr(recovery.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(recovery, "DEFAULT_BASE_PATH", home / "empty-default")
    monkeypatch.setattr(
        recovery, "load_destination_config", lambda _path: DestinationConfig({}, (), home)
    )
    lexical = _capture(home)
    outside = _capture(tmp_path / "outside")
    lexical.unlink()
    lexical.symlink_to(outside)

    assert discover_native_artifacts([home]) == [lexical]

    runner = RecoveryRunner(state_dir=home / "state", job_id="symlink")
    try:
        summary = runner.run([home], dry_run=True)
    finally:
        runner.close()

    assert summary.inventory == {"quarantined": 1}


def test_recovery_payload_has_provenance_but_never_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots: None
) -> None:
    artifact = _capture(tmp_path, working_dir="/workspace/recorded")
    destination = Destination("target", "https://target.example", "target-key", ("**",), ())
    monkeypatch.setattr(recovery, "load_destination_config", lambda _path: _config(destination))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"capabilities": ["native-recovery"]})
        return httpx.Response(202, json={"status": "queued"})

    runner = RecoveryRunner(
        state_dir=tmp_path / "state",
        job_id="payload",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        summary = runner.run([tmp_path])
    finally:
        runner.close()

    payload = json.loads(requests[-1].content)
    assert summary.pending is False
    assert payload["origin"]["session_id"] == "session-1"
    assert payload["origin"]["ordinal"] == 0
    assert len(payload["origin"]["source_line_sha256"]) == 64
    assert len(payload["origin"]["source_stream_sha256"]) == 64
    assert payload["working_dir"] == "/workspace/recorded"
    assert str(artifact) not in requests[-1].content.decode()
    assert "path" not in payload["origin"]


def test_lost_ack_resumes_the_exact_checkpointed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots: None
) -> None:
    _capture(tmp_path)
    destination = Destination("target", "https://target.example", "target-key", ("**",), ())
    monkeypatch.setattr(recovery, "load_destination_config", lambda _path: _config(destination))
    first_payloads: list[bytes] = []

    def lost_ack(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"capabilities": ["native-recovery"]})
        first_payloads.append(request.content)
        raise httpx.RemoteProtocolError("response lost after acceptance")

    first = RecoveryRunner(
        state_dir=tmp_path / "state",
        job_id="resume",
        client=httpx.Client(transport=httpx.MockTransport(lost_ack)),
    )
    try:
        assert first.run([tmp_path]).pending is True
    finally:
        first.close()

    second_payloads: list[bytes] = []

    def duplicate(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"capabilities": ["native-recovery"]})
        second_payloads.append(request.content)
        return httpx.Response(202, json={"status": "duplicate"})

    second = RecoveryRunner(
        state_dir=tmp_path / "state",
        job_id="resume",
        client=httpx.Client(transport=httpx.MockTransport(duplicate)),
    )
    try:
        summary = second.run([tmp_path])
    finally:
        second.close()

    assert summary.pending is False
    assert first_payloads == second_payloads


def test_source_drift_cannot_send_a_pending_checkpointed_record(
    tmp_path: Path, configured_roots: None
) -> None:
    events = _capture(tmp_path)
    first = RecoveryRunner(state_dir=tmp_path / "state", job_id="source-drift")
    try:
        assert first.run([tmp_path], dry_run=True).pending is True
    finally:
        first.close()

    events.write_bytes(b"{malformed}\n")
    requests: list[str] = []
    second = RecoveryRunner(
        state_dir=tmp_path / "state",
        job_id="source-drift",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request.url.path) or httpx.Response(202)
            )
        ),
    )
    try:
        summary = second.run([tmp_path])
    finally:
        second.close()

    assert summary.inventory == {"quarantined": 1}
    assert requests == []


def test_policy_replacement_and_removal_retire_unfinished_delivery_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots: None
) -> None:
    _capture(tmp_path)
    old = Destination("target", "https://old.example", "test-key", ("**",), ())
    replacement = Destination("target", "https://new.example", "test-key", ("**",), ())
    effective = {"config": _config(old)}
    monkeypatch.setattr(
        recovery,
        "load_effective_destination_config",
        lambda _working_dir, _settings_path: (effective["config"], None),
    )

    first = RecoveryRunner(state_dir=tmp_path / "state", job_id="policy")
    try:
        assert first.run([tmp_path], dry_run=True).pending is True
    finally:
        first.close()

    effective["config"] = _config(replacement)
    second = RecoveryRunner(state_dir=tmp_path / "state", job_id="policy")
    try:
        assert second.run([tmp_path], dry_run=True).pending is True
        states = second.checkpoint.connection.execute(
            "SELECT state FROM delivery ORDER BY destination"
        ).fetchall()
        assert sorted(row[0] for row in states) == ["not-required-by-current-policy", "pending"]
    finally:
        second.close()

    effective["config"] = _config()
    third = RecoveryRunner(state_dir=tmp_path / "state", job_id="policy")
    try:
        summary = third.run([tmp_path], dry_run=True)
        states = third.checkpoint.connection.execute(
            "SELECT state FROM delivery ORDER BY destination"
        ).fetchall()
    finally:
        third.close()

    assert summary.inventory == {"not-required-by-current-policy": 1}
    assert summary.pending is False
    assert sorted(row[0] for row in states) == [
        "not-required-by-current-policy",
        "not-required-by-current-policy",
    ]


def test_capability_absence_and_throttle_never_post_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots: None
) -> None:
    _capture(tmp_path)
    destination = Destination("target", "https://target.example", "target-key", ("**",), ())
    monkeypatch.setattr(recovery, "load_destination_config", lambda _path: _config(destination))

    for job_id, version_response in (
        ("absent", httpx.Response(200, json={"capabilities": []})),
        ("throttled", httpx.Response(429, headers={"Retry-After": "7"})),
    ):
        paths: list[str] = []

        def handler(
            request: httpx.Request,
            paths: list[str] = paths,
            version_response: httpx.Response = version_response,
        ) -> httpx.Response:
            paths.append(request.url.path)
            return version_response

        runner = RecoveryRunner(
            state_dir=tmp_path / f"state-{job_id}",
            job_id=job_id,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            summary = runner.run([tmp_path])
        finally:
            runner.close()
        assert paths == ["/version"]
        assert summary.pending is True
        if job_id == "throttled":
            assert summary.retry_after_s is not None and summary.retry_after_s > 0


def test_command_runs_from_the_module_without_server_overrides(
    tmp_path: Path, configured_roots: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture(tmp_path)

    with pytest.raises(SystemExit) as exited:
        cli.main(["--once", "--dry-run", "--path", str(tmp_path)])

    result = json.loads(capsys.readouterr().out)
    assert exited.value.code == 1
    assert result["inventory"] == {"eligible": 1}


def test_command_rejects_direct_server_overrides() -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--server-url", "https://not-accepted.example"])

    assert exited.value.code == 2


def test_scan_roots_deduplicate_and_reject_other_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home" / "owner"
    home.mkdir(parents=True)
    monkeypatch.setattr(recovery.Path, "home", classmethod(lambda _cls: home))
    config = DestinationConfig({}, (), home / ".amplifier" / "projects")
    monkeypatch.setattr(recovery, "DEFAULT_BASE_PATH", config.base_path)

    roots = resolve_scan_roots(
        config, [home / ".amplifier" / "projects", home.parent, home / "captures"]
    )

    assert roots == [home / ".amplifier" / "projects", home / "captures"]
