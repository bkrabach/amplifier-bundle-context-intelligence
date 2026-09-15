"""Tests for the agent-facing native transcript tool."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from amplifier_module_tool_context_intelligence_query.session_transcript_tool import (
    SessionTranscriptTool,
)


def _capture(tmp_path: Path, session_id: str = "session-123") -> Path:
    capture_dir = tmp_path / session_id / "context-intelligence"
    capture_dir.mkdir(parents=True)
    (capture_dir / "metadata.json").write_text(
        json.dumps(
            {
                "format": "context-intelligence",
                "version": "1.0.0",
                "session_id": session_id,
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


def _coordinator(session_id: str, capture_dir: Path) -> MagicMock:
    coordinator = MagicMock()
    coordinator.session_id = session_id
    hook_resolver = SimpleNamespace(session_dir=lambda requested_id: capture_dir)
    coordinator.get_capability.side_effect = lambda name: (
        hook_resolver if name == "context_intelligence.hook_config_resolver" else None
    )
    return coordinator


async def test_omitted_session_ids_uses_runtime_current_session(tmp_path) -> None:
    capture_dir = _capture(tmp_path)
    tool = SessionTranscriptTool(_coordinator("session-123", capture_dir))

    result = await tool.execute({})

    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["sessions"][0]["session_id"] == "session-123"
    assert "[USER | event-line 1]" in result.output["text"]


async def test_explicit_session_ids_use_capture_resolver_capability(tmp_path) -> None:
    capture_dir = _capture(tmp_path, "other-session")
    resolver = SimpleNamespace(
        resolve_capture=lambda requested_id: {
            "events_path": str(capture_dir / "events.jsonl"),
            "metadata_path": str(capture_dir / "metadata.json"),
        }
    )
    coordinator = MagicMock()
    coordinator.session_id = "current-session"
    coordinator.get_capability.side_effect = lambda name: (
        resolver if name == "context_intelligence.capture_resolver" else None
    )
    tool = SessionTranscriptTool(coordinator)

    result = await tool.execute({"session_ids": ["other-session"], "format": "json"})

    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["sessions"][0]["session_id"] == "other-session"
    assert "text" not in result.output


async def test_missing_current_session_identity_fails_loudly() -> None:
    coordinator = MagicMock(spec=[])
    coordinator.get_capability = MagicMock(return_value=None)
    tool = SessionTranscriptTool(coordinator)

    result = await tool.execute({})

    assert result.success is False
    assert isinstance(result.error, dict)
    assert result.error["type"] == "current_session_unavailable"


async def test_invalid_tool_input_returns_a_structured_error() -> None:
    tool = SessionTranscriptTool(MagicMock())

    result = await tool.execute({"format": []})

    assert result.success is False
    assert isinstance(result.error, dict)
    assert result.error["type"] == "invalid_request"


async def test_rejects_path_like_session_ids_before_resolving_a_capture() -> None:
    coordinator = MagicMock()
    tool = SessionTranscriptTool(coordinator)

    result = await tool.execute({"session_ids": ["../other-session"]})

    assert result.success is False
    assert isinstance(result.error, dict)
    assert result.error["type"] == "invalid_request"
    coordinator.get_capability.assert_not_called()


async def test_caches_the_capture_resolver_for_multiple_requested_sessions(tmp_path) -> None:
    first = _capture(tmp_path, "first-session")
    other_capture_root = tmp_path / "other"
    other_capture_root.mkdir()
    second = _capture(other_capture_root, "second-session")
    paths = {"first-session": first, "second-session": second}
    resolver = SimpleNamespace(
        resolve_capture=lambda session_id: {
            "events_path": str(paths[session_id] / "events.jsonl"),
            "metadata_path": str(paths[session_id] / "metadata.json"),
        }
    )
    coordinator = MagicMock()
    coordinator.get_capability.return_value = resolver
    tool = SessionTranscriptTool(coordinator)

    result = await tool.execute({"session_ids": ["first-session", "second-session"]})

    assert result.success is True
    coordinator.get_capability.assert_called_once_with("context_intelligence.capture_resolver")


async def test_multiple_sessions_accept_independent_pagination_cursors(tmp_path) -> None:
    first = _capture(tmp_path, "first-session")
    other_capture_root = tmp_path / "other"
    other_capture_root.mkdir()
    second = _capture(other_capture_root, "second-session")
    paths = {"first-session": first, "second-session": second}
    resolver = SimpleNamespace(
        resolve_capture=lambda session_id: {
            "events_path": str(paths[session_id] / "events.jsonl"),
            "metadata_path": str(paths[session_id] / "metadata.json"),
        }
    )
    coordinator = MagicMock()
    coordinator.get_capability.return_value = resolver

    result = await SessionTranscriptTool(coordinator).execute(
        {
            "session_ids": ["first-session", "second-session"],
            "after_event_lines": {"first-session": 1, "second-session": 0},
        }
    )

    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["sessions"][0]["messages"] == []
    assert result.output["sessions"][1]["messages"][0]["content"] == "Hello"


async def test_hook_fallback_finds_a_named_capture_in_another_project(tmp_path) -> None:
    _capture(tmp_path / "other-project" / "sessions", "other-session")
    hook_resolver = SimpleNamespace(
        base_path=tmp_path,
        session_dir=lambda session_id: tmp_path / "current-project" / "sessions" / session_id,
    )
    coordinator = MagicMock()
    coordinator.get_capability.side_effect = lambda name: (
        hook_resolver if name == "context_intelligence.hook_config_resolver" else None
    )

    result = await SessionTranscriptTool(coordinator).execute(
        {"session_ids": ["other-session"], "format": "json"}
    )

    assert result.success is True
    assert isinstance(result.output, dict)
    assert result.output["sessions"][0]["session_id"] == "other-session"
