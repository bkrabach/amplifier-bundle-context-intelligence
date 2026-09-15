"""Tests for the context-intelligence transcript CLI command."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "context-intelligence.py"


def _script_module():
    spec = importlib.util.spec_from_file_location(
        "context_intelligence_cli_transcript", SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(tmp_path) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    (capture_dir / "metadata.json").write_text(
        json.dumps(
            {
                "format": "context-intelligence",
                "version": "1.0.0",
                "session_id": "session-123",
                "workspace": "workspace-a",
            }
        ),
        encoding="utf-8",
    )
    (capture_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "prompt:submit",
                "workspace": "workspace-a",
                "timestamp": "2026-09-15T10:00:00Z",
                "data": {"prompt": "Hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return capture_dir


def test_transcript_command_renders_explicit_capture_directory(tmp_path, capsys) -> None:
    module = _script_module()

    result = module.main(["transcript", "--session-dir", str(_capture(tmp_path))])

    captured = capsys.readouterr()
    assert result == 0
    assert "[USER | event-line 1]" in captured.out
    assert "Hello" in captured.out


def test_transcript_command_default_content_limit_accepts_normal_message(tmp_path, capsys) -> None:
    module = _script_module()
    capture = _capture(tmp_path)
    (capture / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "prompt:submit",
                "workspace": "workspace-a",
                "timestamp": "2026-09-15T10:00:00Z",
                "data": {"prompt": "x" * 70},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = module.main(["transcript", "--session-dir", str(capture)])

    assert result == 0
    assert ("x" * 70) in capsys.readouterr().out


def test_transcript_command_emits_json_for_multiple_captures(tmp_path, capsys) -> None:
    module = _script_module()
    first = _capture(tmp_path)
    second_parent = tmp_path / "other"
    second_parent.mkdir()
    second = _capture(second_parent)

    result = module.main(
        [
            "transcript",
            "--format",
            "json",
            "--session-dir",
            str(first),
            "--session-dir",
            str(second),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert len(payload["sessions"]) == 2
    assert payload["status"] == "complete"
