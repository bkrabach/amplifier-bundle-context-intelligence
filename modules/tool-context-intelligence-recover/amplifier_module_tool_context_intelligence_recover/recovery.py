"""Native capture recovery with durable, owner-local checkpoints.

This module intentionally has no dependency on the generic upload command.  It
only inventories the hook's ``context-intelligence/events.jsonl`` captures and
uses the hook's destination resolver and routing functions directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from amplifier_module_hook_context_intelligence.config_resolver import (
    Destination,
    HookConfigResolver,
)
from amplifier_module_hook_context_intelligence.fanout import (
    destination_is_active,
    normalize_match_key,
)
from amplifier_module_hook_context_intelligence.keys_env import load_keys_env_into_environ
from amplifier_module_hook_context_intelligence.upload import build_payload
from context_intelligence.auth import build_auth_strategy
from context_intelligence.config import (
    DEFAULT_BASE_PATH,
    SETTINGS_PATH,
    _expand_env_placeholders,
    read_hook_config_block,
)

_NATIVE_FORMAT = "context-intelligence"
_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed"})
_PREFIX_BYTES = 4096


@dataclass(frozen=True)
class DestinationConfig:
    """Resolved destinations plus names the hook resolver rejected."""

    destinations: dict[str, Destination]
    invalid_names: tuple[str, ...]
    base_path: Path


@dataclass(frozen=True)
class NativeRecord:
    ordinal: int
    line_sha256: str
    payload: dict[str, Any]


@dataclass
class Artifact:
    artifact_id: str
    path: Path
    state: str
    reason: str = ""
    session_id: str = ""
    working_dir: str = ""
    parent_id: str = ""
    records: list[NativeRecord] | None = None


@dataclass(frozen=True)
class RecoverySummary:
    """Local-only summary; deliberately contains no source paths."""

    inventory: dict[str, int]
    delivered: int
    pending: bool
    retry_after_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "inventory": dict(sorted(self.inventory.items())),
            "delivered": self.delivered,
            "pending": self.pending,
        }
        if self.retry_after_s is not None:
            result["retry_after_s"] = self.retry_after_s
        return result


def _expand_config(config: dict[str, Any]) -> dict[str, Any]:
    """Expand only connection fields, matching the standalone hook consumer."""

    expanded = dict(config)
    for key in (
        "base_path",
        "context_intelligence_server_url",
        "context_intelligence_api_key",
    ):
        if isinstance(expanded.get(key), str):
            expanded[key] = _expand_env_placeholders(expanded[key])
    raw_destinations = expanded.get("destinations")
    if isinstance(raw_destinations, dict):
        rebuilt: dict[str, Any] = {}
        for name, spec in raw_destinations.items():
            if not isinstance(spec, dict):
                rebuilt[name] = spec
                continue
            copied = dict(spec)
            for key in ("url", "api_key", "auth_resource"):
                if isinstance(copied.get(key), str):
                    copied[key] = _expand_env_placeholders(copied[key])
            rebuilt[name] = copied
        expanded["destinations"] = rebuilt
    return expanded


def _read_settings_document(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read one settings document, distinguishing absence from an unsafe failure."""

    try:
        path_stat = path.stat()
    except FileNotFoundError:
        return {}, None
    except OSError:
        return {}, "settings-unreadable"
    if not stat.S_ISREG(path_stat.st_mode):
        return {}, "settings-path-is-not-a-file"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}, "settings-unreadable"
    except yaml.YAMLError:
        return {}, "settings-malformed"
    if loaded is None:
        return {}, None
    if not isinstance(loaded, dict):
        return {}, "settings-not-a-mapping"
    return loaded, None


def _recorded_project_dir(working_dir: str) -> tuple[Path | None, str | None]:
    """Confirm the recorded directory exists before consulting its local settings."""

    project = Path(working_dir)
    try:
        if not stat.S_ISDIR(project.stat().st_mode):
            return None, "recorded-working-dir-unavailable"
    except (OSError, UnicodeError):
        return None, "recorded-working-dir-unavailable"
    return project, None


def _deep_merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Match Amplifier settings layering: mappings merge recursively; scalars replace."""

    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def _hook_config_from_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Extract the hook configuration without accepting malformed configuration structure."""

    overrides = settings.get("overrides", {})
    if not isinstance(overrides, dict):
        return {}, "overrides-not-a-mapping"
    hook_override = overrides.get("hook-context-intelligence")
    if hook_override is None:
        return {}, None
    if not isinstance(hook_override, dict):
        return {}, "hook-override-not-a-mapping"
    config = hook_override.get("config", {})
    if not isinstance(config, dict):
        return {}, "hook-config-not-a-mapping"
    return config, None


def _destination_config_from_hook_config(config: dict[str, Any]) -> DestinationConfig:
    """Resolve a validated destination set using the hook's implementation."""

    expanded = _expand_config(config)
    resolver = HookConfigResolver(expanded, None)
    raw_destinations = expanded.get("destinations")
    raw_names = set(raw_destinations) if isinstance(raw_destinations, dict) else set()
    all_names = raw_names | set(resolver.destinations)
    valid = resolver.validate_destinations()
    configured_base = resolver.base_path
    return DestinationConfig(
        destinations=valid,
        invalid_names=tuple(sorted(all_names - set(valid))),
        base_path=configured_base,
    )


def load_destination_config(settings_path: Path = SETTINGS_PATH) -> DestinationConfig:
    """Resolve the user-level hook configuration used to determine scan roots."""

    load_keys_env_into_environ()
    raw = _expand_config(read_hook_config_block(settings_path))
    return _destination_config_from_hook_config(raw)


def load_effective_destination_config(
    working_dir: str, settings_path: Path = SETTINGS_PATH
) -> tuple[DestinationConfig | None, str | None]:
    """Resolve user settings merged with the recorded project's local settings.

    A missing local file is a valid configuration layer. A missing, non-directory,
    unreadable, or malformed recorded project cannot prove that its local layer is
    absent, so recovery fails closed instead of applying the user-level layer alone.
    """

    load_keys_env_into_environ()
    global_settings, error = _read_settings_document(settings_path)
    if error is not None:
        return None, f"global-{error}"

    project, error = _recorded_project_dir(working_dir)
    if error is not None or project is None:
        return None, error
    local_settings, error = _read_settings_document(project / ".amplifier" / "settings.yaml")
    if error is not None:
        return None, f"project-{error}"

    hook_config, error = _hook_config_from_settings(
        _deep_merge_settings(global_settings, local_settings)
    )
    if error is not None:
        return None, error
    return _destination_config_from_hook_config(hook_config), None


def resolve_scan_roots(
    destination_config: DestinationConfig, explicit_paths: Iterable[Path] = ()
) -> list[Path]:
    """Return deduplicated, current-user roots without traversing other homes."""

    candidates = [destination_config.base_path, DEFAULT_BASE_PATH, *explicit_paths]
    roots: list[Path] = []
    for raw in candidates:
        path = Path(raw).expanduser().absolute()
        if _is_other_users_home(path):
            continue
        if path not in roots:
            roots.append(path)
    return roots


def _is_other_users_home(path: Path) -> bool:
    """Prevent a broad root such as ``/home`` from traversing another user."""

    home = Path.home().resolve()
    parent = home.parent
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return False
    return not relative.parts or relative.parts[0] != home.name


def discover_native_artifacts(roots: Iterable[Path]) -> list[Path]:
    """Discover lexical native capture candidates, never metadata-led candidates."""

    found: set[Path] = set()
    for root in roots:
        try:
            if root.is_file() and _is_native_capture_path(root):
                found.add(root)
            elif root.is_dir():
                found.update(
                    path
                    for path in root.rglob(f"{_NATIVE_FORMAT}/events.jsonl")
                    if _is_native_capture_path(path)
                )
        except OSError:
            # An inaccessible root has no discoverable artifact to inventory.
            continue
    return sorted(found)


def _is_native_capture_path(path: Path) -> bool:
    """Accept the precise layout emitted by the Context Intelligence hook."""

    return (
        path.name == "events.jsonl"
        and path.parent.name == _NATIVE_FORMAT
        and path.parent.parent.parent.name == "sessions"
    )


def _target_is_outside_owner_home(path: Path) -> bool:
    """Reject a lexical candidate whose symlink-resolved target escapes the owner."""

    try:
        path.resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        return True
    return False


def _artifact_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _source_stream_sha256(line_hashes: Iterable[str]) -> str:
    """Hash ordered line digests, never an implicit platform text representation."""

    hasher = hashlib.sha256()
    for line_hash in line_hashes:
        hasher.update(line_hash.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _prefix_digest(path: Path, size: int) -> str:
    with path.open("rb") as stream:
        return hashlib.sha256(stream.read(size)).hexdigest()


class RecoveryCheckpoint:
    """SQLite state owned by the invoking user, with no source mutation."""

    def __init__(self, state_dir: Path, job_id: str) -> None:
        self.state_dir = state_dir.expanduser()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.path = self.state_dir / f"{job_id}.sqlite3"
        self.connection = sqlite3.connect(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact (
              artifact_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
              device INTEGER, inode INTEGER, size INTEGER, mtime_ns INTEGER,
              prefix_sha256 TEXT, scan_offset INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              session_id TEXT NOT NULL DEFAULT '', working_dir TEXT NOT NULL DEFAULT '',
              parent_id TEXT NOT NULL DEFAULT '', order_index INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS record (
              artifact_id TEXT NOT NULL REFERENCES artifact(artifact_id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL, line_sha256 TEXT NOT NULL,
              source_stream_sha256 TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              payload_json TEXT NOT NULL, PRIMARY KEY (artifact_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS delivery (
              artifact_id TEXT NOT NULL REFERENCES artifact(artifact_id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL, destination TEXT NOT NULL,
              policy_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
              retry_at REAL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (artifact_id, ordinal, destination)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def prior(self, artifact_id: str) -> sqlite3.Row | None:
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()

    def reset_records(self, artifact_id: str) -> None:
        self.connection.execute("DELETE FROM record WHERE artifact_id = ?", (artifact_id,))

    def records(self, artifact_id: str) -> list[NativeRecord]:
        rows = self.connection.execute(
            "SELECT ordinal, line_sha256, payload_json FROM record WHERE artifact_id = ? ORDER BY ordinal",
            (artifact_id,),
        ).fetchall()
        return [
            NativeRecord(ordinal=row[0], line_sha256=row[1], payload=json.loads(row[2]))
            for row in rows
        ]

    def save_artifact(
        self, artifact: Artifact, path_stat: os.stat_result, prefix: str, offset: int
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO artifact (
              artifact_id, path, device, inode, size, mtime_ns, prefix_sha256, scan_offset,
              state, reason, session_id, working_dir, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              path=excluded.path, device=excluded.device, inode=excluded.inode,
              size=excluded.size, mtime_ns=excluded.mtime_ns, prefix_sha256=excluded.prefix_sha256,
              scan_offset=excluded.scan_offset, state=excluded.state, reason=excluded.reason,
              session_id=excluded.session_id, working_dir=excluded.working_dir, parent_id=excluded.parent_id
            """,
            (
                artifact.artifact_id,
                str(artifact.path),
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
                path_stat.st_mtime_ns,
                prefix,
                offset,
                artifact.state,
                artifact.reason,
                artifact.session_id,
                artifact.working_dir,
                artifact.parent_id,
            ),
        )

    def update_artifact_state(self, artifact: Artifact) -> None:
        self.connection.execute(
            """
            UPDATE artifact SET state = ?, reason = ?, session_id = ?, working_dir = ?, parent_id = ?
            WHERE artifact_id = ?
            """,
            (
                artifact.state,
                artifact.reason,
                artifact.session_id,
                artifact.working_dir,
                artifact.parent_id,
                artifact.artifact_id,
            ),
        )

    def set_order(self, artifacts: list[Artifact]) -> None:
        for index, artifact in enumerate(artifacts):
            self.connection.execute(
                "UPDATE artifact SET order_index = ? WHERE artifact_id = ?",
                (index, artifact.artifact_id),
            )

    def save_inaccessible(self, artifact: Artifact) -> None:
        self.connection.execute(
            """
            INSERT INTO artifact (artifact_id, path, state, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET state=excluded.state, reason=excluded.reason
            """,
            (artifact.artifact_id, str(artifact.path), artifact.state, artifact.reason),
        )

    def save_records(self, artifact_id: str, records: list[NativeRecord]) -> None:
        stream_hash = _source_stream_sha256(record.line_sha256 for record in records)
        self.connection.execute(
            "UPDATE record SET source_stream_sha256 = ? WHERE artifact_id = ?",
            (stream_hash, artifact_id),
        )
        for record in records:
            self.connection.execute(
                """
                INSERT INTO record (
                  artifact_id, ordinal, line_sha256, source_stream_sha256, idempotency_key, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id, ordinal) DO UPDATE SET
                  line_sha256=excluded.line_sha256, source_stream_sha256=excluded.source_stream_sha256,
                  idempotency_key=excluded.idempotency_key, payload_json=excluded.payload_json
                """,
                (
                    artifact_id,
                    record.ordinal,
                    record.line_sha256,
                    stream_hash,
                    record.payload["idempotency_key"],
                    json.dumps(record.payload, sort_keys=True, separators=(",", ":")),
                ),
            )

    def ensure_delivery(
        self,
        artifact_id: str,
        ordinal: int,
        destination: str,
        fingerprint: str,
        state: str,
        error: str = "",
    ) -> None:
        prior = self.connection.execute(
            """
            SELECT policy_fingerprint, state, retry_at FROM delivery
            WHERE artifact_id = ? AND ordinal = ? AND destination = ?
            """,
            (artifact_id, ordinal, destination),
        ).fetchone()
        if prior is not None and prior[0] == fingerprint:
            if prior[1] in {"delivered", "duplicate", "quarantined"}:
                return
            if prior[1] == "deferred" and prior[2] is not None and prior[2] > time.time():
                return
        self.connection.execute(
            """
            INSERT INTO delivery (artifact_id, ordinal, destination, policy_fingerprint, state, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id, ordinal, destination) DO UPDATE SET
              policy_fingerprint=excluded.policy_fingerprint, state=excluded.state,
              retry_at=NULL, error=excluded.error
            """,
            (artifact_id, ordinal, destination, fingerprint, state, error),
        )

    def update_delivery(
        self,
        artifact_id: str,
        ordinal: int,
        destination: str,
        state: str,
        *,
        retry_at: float | None = None,
        error: str = "",
    ) -> None:
        self.connection.execute(
            """
            UPDATE delivery SET state = ?, retry_at = ?, attempts = attempts + 1, error = ?
            WHERE artifact_id = ? AND ordinal = ? AND destination = ?
            """,
            (state, retry_at, error, artifact_id, ordinal, destination),
        )

    def pending_deliveries(self, destination: str) -> list[sqlite3.Row]:
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            """
            SELECT d.artifact_id, d.ordinal, r.payload_json, r.source_stream_sha256, r.line_sha256
            FROM delivery d
            JOIN record r ON d.artifact_id = r.artifact_id AND d.ordinal = r.ordinal
            JOIN artifact a ON d.artifact_id = a.artifact_id
            WHERE d.destination = ? AND a.state = 'eligible' AND d.state IN ('pending', 'deferred')
              AND (d.retry_at IS NULL OR d.retry_at <= ?)
            ORDER BY a.order_index, d.ordinal
            """,
            (destination, time.time()),
        ).fetchall()

    def retire_unrequired(
        self, artifact_ids: Iterable[str], required: set[tuple[str, int, str]]
    ) -> None:
        """Retain obsolete unfinished work, but remove it from delivery eligibility."""

        for artifact_id in artifact_ids:
            rows = self.connection.execute(
                """
                SELECT artifact_id, ordinal, destination FROM delivery
                WHERE artifact_id = ? AND state IN ('pending', 'deferred', 'blocked', 'quarantined')
                """,
                (artifact_id,),
            ).fetchall()
            for row in rows:
                key = (str(row[0]), int(row[1]), str(row[2]))
                if key not in required:
                    self.connection.execute(
                        """
                        UPDATE delivery
                        SET state = 'not-required-by-current-policy', retry_at = NULL,
                            error = 'not-required-by-current-policy'
                        WHERE artifact_id = ? AND ordinal = ? AND destination = ?
                        """,
                        row,
                    )

    def has_deliveries(self, artifact_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM delivery WHERE artifact_id = ? LIMIT 1", (artifact_id,)
        ).fetchone()
        return row is not None

    def outstanding(self) -> tuple[bool, float | None]:
        row = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM delivery
               WHERE state IN ('pending', 'deferred', 'blocked', 'quarantined'))
              + (SELECT COUNT(*) FROM artifact
                 WHERE state IN ('deferred', 'blocked', 'quarantined')),
              MIN(retry_at)
            FROM delivery WHERE state = 'deferred'
            """
        ).fetchone()
        return bool(row[0]), row[1]

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) FROM artifact GROUP BY state"
        ).fetchall()
        return {str(state): int(count) for state, count in rows}

    def commit(self) -> None:
        self.connection.commit()


class RecoveryRunner:
    """Inventory, route, and deliver native captures to every valid destination."""

    def __init__(
        self,
        *,
        state_dir: Path,
        job_id: str | None = None,
        settings_path: Path = SETTINGS_PATH,
        client: httpx.Client | None = None,
    ) -> None:
        self.job_id = job_id or str(uuid.uuid4())
        self.settings_path = settings_path
        self.checkpoint = RecoveryCheckpoint(state_dir, self.job_id)
        self.client = client

    def close(self) -> None:
        self.checkpoint.close()

    def run(self, explicit_paths: Iterable[Path] = (), *, dry_run: bool = False) -> RecoverySummary:
        scan_config = load_destination_config(self.settings_path)
        artifacts = [
            self._scan_artifact(path)
            for path in discover_native_artifacts(resolve_scan_roots(scan_config, explicit_paths))
        ]
        self._mark_duplicates(artifacts)
        self._mark_parent_cycles(artifacts)
        ordered = self._parent_first(artifacts)
        self.checkpoint.set_order(ordered)
        policies: dict[str, Destination] = {}
        required: set[tuple[str, int, str]] = set()
        for artifact in ordered:
            self._route(artifact, policies, required)
            self.checkpoint.update_artifact_state(artifact)
        self.checkpoint.retire_unrequired(
            (artifact.artifact_id for artifact in artifacts),
            required,
        )
        self.checkpoint.commit()

        delivered = 0
        retry_after: float | None = None
        if not dry_run:
            owns_client = self.client is None
            client = self.client or httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0))
            try:
                for destination_key, destination in policies.items():
                    delivered_now, retry = self._deliver_destination(
                        client, destination_key, destination
                    )
                    delivered += delivered_now
                    if retry is not None:
                        retry_after = retry if retry_after is None else min(retry_after, retry)
            finally:
                if owns_client:
                    client.close()
            self._refresh_artifact_states()
            self.checkpoint.commit()

        pending, retry_at = self.checkpoint.outstanding()
        if retry_at is not None and retry_at > time.time():
            wait = retry_at - time.time()
            retry_after = wait if retry_after is None else min(retry_after, wait)
        return RecoverySummary(self.checkpoint.counts(), delivered, pending, retry_after)

    def _scan_artifact(self, path: Path) -> Artifact:
        artifact_id = _artifact_id(path)
        artifact = Artifact(
            artifact_id=artifact_id, path=path, state="deferred", reason="unscanned"
        )
        try:
            if _target_is_outside_owner_home(path):
                artifact.state, artifact.reason = "quarantined", "target-outside-owner-home"
                self.checkpoint.save_inaccessible(artifact)
                return artifact
            path_stat = path.stat()
            if not stat.S_ISREG(path_stat.st_mode):
                artifact.state, artifact.reason = "quarantined", "not-a-regular-file"
                self.checkpoint.save_artifact(artifact, path_stat, "", 0)
                return artifact
            prior = self.checkpoint.prior(artifact_id)
            rescan = prior is None
            if prior is not None:
                old_offset = int(prior["scan_offset"])
                old_identity = (prior["device"], prior["inode"])
                current_identity = (path_stat.st_dev, path_stat.st_ino)
                if (
                    path_stat.st_size < old_offset
                    or old_identity != current_identity
                    or (
                        path_stat.st_size <= old_offset
                        and path_stat.st_mtime_ns != prior["mtime_ns"]
                    )
                    or _prefix_digest(path, min(old_offset, _PREFIX_BYTES))
                    != prior["prefix_sha256"]
                ):
                    rescan = True
                elif prior["reason"] == "malformed-line":
                    # A malformed complete source line cannot become valid by
                    # appending; revalidate the whole stream rather than
                    # forgetting the earlier quarantine result.
                    rescan = True
            if rescan:
                self.checkpoint.reset_records(artifact_id)
                offset, records = 0, []
            else:
                assert prior is not None
                offset, records = int(prior["scan_offset"]), self.checkpoint.records(artifact_id)
            records, offset, line_error = self._read_complete_suffix(path, offset, records)
            metadata, metadata_error = self._read_metadata(path.parent / "metadata.json")
            artifact.records = records
            if metadata_error:
                artifact.state, artifact.reason = metadata_error
            elif line_error:
                artifact.state, artifact.reason = "quarantined", line_error
            else:
                self._validate_metadata(artifact, metadata)
            prefix = _prefix_digest(path, min(offset, _PREFIX_BYTES))
            self.checkpoint.save_artifact(artifact, path_stat, prefix, offset)
            if artifact.records is not None:
                self.checkpoint.save_records(artifact_id, artifact.records)
            return artifact
        except (OSError, UnicodeError):
            artifact.state, artifact.reason = "quarantined", "inaccessible-file"
            self.checkpoint.save_inaccessible(artifact)
            return artifact

    def _read_complete_suffix(
        self, path: Path, offset: int, existing: list[NativeRecord]
    ) -> tuple[list[NativeRecord], int, str | None]:
        records = list(existing)
        with path.open("rb") as stream:
            stream.seek(offset)
            next_offset = offset
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    return records, next_offset, "incomplete-line"
                next_offset = stream.tell()
                body = raw_line.rstrip(b"\r\n")
                if not body:
                    return records, next_offset, "malformed-line"
                digest = hashlib.sha256(raw_line).hexdigest()
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return records, next_offset, "malformed-line"
                if not isinstance(parsed, dict) or not isinstance(parsed.get("event"), str):
                    return records, next_offset, "malformed-line"
                if not isinstance(parsed.get("data"), dict):
                    return records, next_offset, "malformed-line"
                payload = build_payload(
                    parsed["event"],
                    parsed.get("workspace") if isinstance(parsed.get("workspace"), str) else "",
                    parsed["data"],
                )
                records.append(NativeRecord(len(records), digest, payload))
        return records, next_offset, None

    @staticmethod
    def _read_metadata(path: Path) -> tuple[dict[str, Any], tuple[str, str] | None]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, ("deferred", "missing-metadata")
        except (OSError, UnicodeError):
            return {}, ("quarantined", "inaccessible-metadata")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}, ("quarantined", "corrupt-metadata")
        if not isinstance(parsed, dict):
            return {}, ("quarantined", "corrupt-metadata")
        return parsed, None

    @staticmethod
    def _validate_metadata(artifact: Artifact, metadata: dict[str, Any]) -> None:
        session_id = metadata.get("session_id")
        working_dir = metadata.get("working_dir")
        if metadata.get("format") != _NATIVE_FORMAT:
            artifact.state, artifact.reason = "quarantined", "invalid-native-metadata"
            return
        if (
            not isinstance(session_id, str)
            or not session_id
            or session_id != artifact.path.parent.parent.name
        ):
            artifact.state, artifact.reason = "quarantined", "inconsistent-session-id"
            return
        if metadata.get("status") not in _TERMINAL_STATUSES or not metadata.get("ended_at"):
            artifact.state, artifact.reason = "deferred", "session-incomplete"
            return
        if not isinstance(working_dir, str) or not working_dir:
            artifact.state, artifact.reason = "quarantined", "routing-undecidable"
            return
        parent_id = metadata.get("parent_id", "")
        if not isinstance(parent_id, str):
            artifact.state, artifact.reason = "quarantined", "invalid-parent-order"
            return
        try:
            normalized = normalize_match_key(working_dir)
        except (OSError, ValueError):
            artifact.state, artifact.reason = "quarantined", "routing-undecidable"
            return
        records = artifact.records or []
        if not records:
            artifact.state, artifact.reason = "quarantined", "empty-event-stream"
            return
        if any(record.payload["data"].get("session_id") != session_id for record in records):
            artifact.state, artifact.reason = "quarantined", "inconsistent-session-id"
            return
        artifact.records = [
            NativeRecord(
                record.ordinal,
                record.line_sha256,
                {**record.payload, "working_dir": normalized.removesuffix("/")},
            )
            for record in records
        ]
        artifact.state, artifact.reason = "eligible", ""
        artifact.session_id = session_id
        artifact.working_dir = normalized
        artifact.parent_id = parent_id

    @staticmethod
    def _mark_duplicates(artifacts: list[Artifact]) -> None:
        seen: set[str] = set()
        for artifact in sorted(artifacts, key=lambda item: str(item.path)):
            if artifact.state == "eligible":
                if artifact.session_id in seen:
                    artifact.state, artifact.reason = "deferred", "duplicate-artifact"
                else:
                    seen.add(artifact.session_id)

    @staticmethod
    def _mark_parent_cycles(artifacts: list[Artifact]) -> None:
        """Quarantine impossible parent order while keeping orphan captures visible."""

        eligible = {
            artifact.session_id: artifact for artifact in artifacts if artifact.state == "eligible"
        }
        for artifact in eligible.values():
            seen: set[str] = set()
            current = artifact
            while current.parent_id in eligible:
                if current.parent_id in seen or current.parent_id == artifact.session_id:
                    artifact.state, artifact.reason = "quarantined", "invalid-parent-order"
                    break
                seen.add(current.session_id)
                current = eligible[current.parent_id]

    @staticmethod
    def _parent_first(artifacts: list[Artifact]) -> list[Artifact]:
        eligible = {
            artifact.session_id: artifact for artifact in artifacts if artifact.state == "eligible"
        }
        children: dict[str, list[Artifact]] = {session_id: [] for session_id in eligible}
        roots: list[Artifact] = []
        for artifact in eligible.values():
            if artifact.parent_id in eligible and artifact.parent_id != artifact.session_id:
                children[artifact.parent_id].append(artifact)
            else:
                roots.append(artifact)
        result: list[Artifact] = []
        todo = sorted(roots, key=lambda item: item.session_id)
        while todo:
            artifact = todo.pop(0)
            result.append(artifact)
            todo.extend(sorted(children[artifact.session_id], key=lambda item: item.session_id))
        # A malformed parent cycle is already not an ordering guarantee; retain it visibly and deterministically.
        result.extend(
            sorted(
                (item for item in artifacts if item not in result), key=lambda item: str(item.path)
            )
        )
        return result

    def _route(
        self,
        artifact: Artifact,
        policies: dict[str, Destination],
        required: set[tuple[str, int, str]],
    ) -> None:
        if artifact.state != "eligible":
            return
        config, config_error = load_effective_destination_config(
            artifact.working_dir.removesuffix("/"), self.settings_path
        )
        if config_error is not None or config is None:
            artifact.state, artifact.reason = (
                "blocked",
                f"effective-config-unavailable:{config_error or 'unknown'}",
            )
            return
        records = artifact.records or []
        if not config.destinations and not config.invalid_names:
            if self.checkpoint.has_deliveries(artifact.artifact_id):
                artifact.state, artifact.reason = "not-required-by-current-policy", ""
            else:
                artifact.state, artifact.reason = "blocked", "no-valid-destinations"
            return
        for name in config.invalid_names:
            fingerprint = hashlib.sha256(f"invalid:{name}".encode()).hexdigest()
            destination_key = _destination_key(name, fingerprint)
            for record in records:
                self.checkpoint.ensure_delivery(
                    artifact.artifact_id,
                    record.ordinal,
                    destination_key,
                    fingerprint,
                    "blocked",
                    "invalid-destination",
                )
                required.add((artifact.artifact_id, record.ordinal, destination_key))
        for name, destination in config.destinations.items():
            fingerprint = _policy_fingerprint(destination)
            destination_key = _destination_key(name, fingerprint)
            try:
                state = (
                    "pending"
                    if destination_is_active(destination, artifact.working_dir)
                    else "filtered"
                )
            except (TypeError, ValueError):
                state = "blocked"
            if state == "pending":
                policies[destination_key] = destination
            for record in records:
                self.checkpoint.ensure_delivery(
                    artifact.artifact_id, record.ordinal, destination_key, fingerprint, state
                )
                required.add((artifact.artifact_id, record.ordinal, destination_key))
        delivery_states = {
            row[0]
            for row in self.checkpoint.connection.execute(
                "SELECT state FROM delivery WHERE artifact_id = ?", (artifact.artifact_id,)
            ).fetchall()
        }
        if delivery_states == {"filtered"}:
            artifact.state, artifact.reason = "filtered", ""
        elif "blocked" in delivery_states and "pending" not in delivery_states:
            artifact.state, artifact.reason = "blocked", "invalid-destination"

    def _deliver_destination(
        self, client: httpx.Client, destination_key: str, destination: Destination
    ) -> tuple[int, float | None]:
        try:
            headers = self._destination_headers(destination)
        except Exception:  # noqa: BLE001 - authentication is an external boundary
            self._set_destination_state(
                destination_key, "blocked", "authentication-unavailable", None
            )
            return 0, None
        available, retry_after = self._has_recovery_capability(client, destination, headers)
        if not available:
            state = "deferred" if retry_after is not None else "blocked"
            error = "throttled" if retry_after is not None else "capability-unavailable"
            self._set_destination_state(destination_key, state, error, retry_after)
            return 0, retry_after
        sent = 0
        for row in self.checkpoint.pending_deliveries(destination_key):
            payload = json.loads(row["payload_json"])
            payload["origin"] = {
                "session_id": self._session_id(row["artifact_id"]),
                "ordinal": row["ordinal"],
                "source_line_sha256": row["line_sha256"],
                "source_stream_sha256": row["source_stream_sha256"],
            }
            try:
                response = client.post(
                    f"{destination.url.rstrip('/')}/recovery/events", json=payload, headers=headers
                )
            except httpx.HTTPError:
                # Leave the exact checkpointed payload pending for lost acknowledgement recovery.
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "pending",
                    error="transport",
                )
                continue
            if response.status_code == 202:
                outcome = _response_outcome(response)
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "duplicate" if outcome == "duplicate" else "delivered",
                )
                sent += 1
            elif response.status_code == 429:
                retry = _retry_after(response)
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "deferred",
                    retry_at=time.time() + retry,
                    error="throttled",
                )
                return sent, retry
            elif response.status_code == 409:
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "quarantined",
                    error="conflict",
                )
            elif response.status_code == 422:
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "quarantined",
                    error="invalid",
                )
            else:
                self.checkpoint.update_delivery(
                    row["artifact_id"],
                    row["ordinal"],
                    destination_key,
                    "blocked",
                    error=f"http-{response.status_code}",
                )
        return sent, None

    @staticmethod
    def _destination_headers(destination: Destination) -> dict[str, str]:
        return build_auth_strategy(
            auth_mode=destination.auth_mode,
            api_key=destination.api_key,
            auth_resource=destination.auth_resource,
        ).headers()

    @staticmethod
    def _has_recovery_capability(
        client: httpx.Client, destination: Destination, headers: dict[str, str]
    ) -> tuple[bool, float | None]:
        try:
            response = client.get(f"{destination.url.rstrip('/')}/version", headers=headers)
        except Exception:  # noqa: BLE001 - version is an external boundary
            return False, None
        if response.status_code == 429:
            return False, _retry_after(response)
        if not 200 <= response.status_code < 300:
            return False, None
        try:
            capabilities = response.json().get("capabilities", [])
        except (ValueError, AttributeError):
            return False, None
        return isinstance(capabilities, list) and "native-recovery" in capabilities, None

    def _set_destination_state(
        self, destination_key: str, state: str, error: str, retry_after: float | None
    ) -> None:
        retry_at = time.time() + retry_after if retry_after is not None else None
        for row in self.checkpoint.pending_deliveries(destination_key):
            self.checkpoint.update_delivery(
                row["artifact_id"],
                row["ordinal"],
                destination_key,
                state,
                retry_at=retry_at,
                error=error,
            )

    def _session_id(self, artifact_id: str) -> str:
        row = self.checkpoint.connection.execute(
            "SELECT session_id FROM artifact WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return str(row[0])

    def _refresh_artifact_states(self) -> None:
        rows = self.checkpoint.connection.execute(
            "SELECT artifact_id, state FROM artifact"
        ).fetchall()
        for artifact_id, state in rows:
            if state not in {"eligible", "blocked"}:
                continue
            delivery_states = {
                item[0]
                for item in self.checkpoint.connection.execute(
                    "SELECT state FROM delivery WHERE artifact_id = ?", (artifact_id,)
                ).fetchall()
            }
            if delivery_states and delivery_states <= {"delivered", "duplicate", "filtered"}:
                final = "duplicate" if delivery_states == {"duplicate"} else "accepted"
                self.checkpoint.connection.execute(
                    "UPDATE artifact SET state = ?, reason = '' WHERE artifact_id = ?",
                    (final, artifact_id),
                )
            elif "blocked" in delivery_states and "pending" not in delivery_states:
                self.checkpoint.connection.execute(
                    "UPDATE artifact SET state = 'blocked', reason = 'destination-blocked' WHERE artifact_id = ?",
                    (artifact_id,),
                )


def _policy_fingerprint(destination: Destination) -> str:
    policy = {
        "name": destination.name,
        "url": destination.url,
        "include": destination.include,
        "exclude": destination.exclude,
        "auth_mode": destination.auth_mode,
        "auth_resource": destination.auth_resource,
    }
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _destination_key(name: str, fingerprint: str) -> str:
    """Keep each destination policy generation independently checkpointed."""

    return f"{name}:{fingerprint}"


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _response_outcome(response: httpx.Response) -> str:
    try:
        status = response.json().get("status")
    except (ValueError, AttributeError):
        return "queued"
    return "duplicate" if status == "duplicate" else "queued"
