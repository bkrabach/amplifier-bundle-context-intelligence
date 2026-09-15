"""Tests for strict local Context Intelligence transcript retrieval."""

from __future__ import annotations

import json

import pytest

from context_intelligence.native_transcript import (
    CaptureLocator,
    NativeTranscriptError,
    TranscriptRequest,
    read_native_transcript,
    render_native_transcript,
)


def _capture(tmp_path, events: list[object], *, metadata: dict | None = None) -> CaptureLocator:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    meta = metadata or {
        "format": "context-intelligence",
        "version": "1.0.0",
        "session_id": "session-123",
        "workspace": "workspace-a",
    }
    (capture_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (capture_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) if not isinstance(event, str) else event for event in events)
        + "\n",
        encoding="utf-8",
    )
    return CaptureLocator.from_session_dir(capture_dir)


def _event(name: str, timestamp: str, **data: object) -> dict:
    return {"event": name, "workspace": "workspace-a", "timestamp": timestamp, "data": data}


def test_reads_verbatim_user_and_assistant_messages(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [
            _event("prompt:submit", "2026-09-15T10:00:00Z", prompt="  preserve\nthis exactly  "),
            _event("prompt:complete", "2026-09-15T10:00:01Z", response="answer\n\nwith spacing"),
        ],
    )

    page = read_native_transcript(locator)

    assert page.status == "complete"
    assert [(message.role, message.content) for message in page.messages] == [
        ("user", "  preserve\nthis exactly  "),
        ("assistant", "answer\n\nwith spacing"),
    ]
    assert [message.event_line for message in page.messages] == [1, 2]


def test_page_boundary_drops_no_message_and_sets_cursor(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [
            _event("prompt:submit", "2026-09-15T10:00:00Z", prompt="one"),
            _event("prompt:complete", "2026-09-15T10:00:01Z", response="two"),
            _event("prompt:submit", "2026-09-15T10:00:02Z", prompt="three"),
        ],
    )

    first = read_native_transcript(locator, TranscriptRequest(max_messages=2))
    second = read_native_transcript(
        locator, TranscriptRequest(after_event_line=first.next_after_event_line or 0)
    )

    assert first.status == "complete"
    assert first.has_more is True
    assert first.next_after_event_line == 2
    assert [message.content for message in first.messages] == ["one", "two"]
    assert [message.content for message in second.messages] == ["three"]


def test_reports_malformed_event_as_capture_issue(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [
            _event("prompt:submit", "2026-09-15T10:00:00Z", prompt="one"),
            "{not valid JSON",
            _event("prompt:complete", "2026-09-15T10:00:01Z", response="two"),
        ],
    )

    page = read_native_transcript(locator)

    assert page.status == "partial"
    assert page.issues[0].code == "malformed_json"
    assert [message.content for message in page.messages] == ["one", "two"]


def test_reports_an_oversized_unrelated_event_without_parsing_it(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [
            _event("prompt:submit", "2026-09-15T10:00:00Z", prompt="one"),
            _event("prompt:complete", "2026-09-15T10:00:01Z", response="two"),
        ],
    )
    with locator.events_path.open("rb") as event_file:
        valid_lines = event_file.read()
    locator.events_path.write_bytes(
        valid_lines.splitlines(keepends=True)[0]
        + b'{"event":"llm:response","data":{"raw":"'
        + (b"x" * 1_000_000)
        + b'"}}\n'
        + valid_lines.splitlines(keepends=True)[1]
    )

    page = read_native_transcript(locator)

    assert page.status == "partial"
    assert page.issues[0].code == "event_too_large_or_invalid_encoding"
    assert [message.content for message in page.messages] == ["one", "two"]


def test_rejects_unknown_capture_schema_before_reading_events(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [],
        metadata={
            "format": "context-intelligence",
            "version": "999.0.0",
            "session_id": "s",
            "workspace": "w",
        },
    )

    with pytest.raises(NativeTranscriptError, match="unsupported capture version"):
        read_native_transcript(locator)


def test_reports_a_missing_events_file_without_a_precheck(tmp_path) -> None:
    locator = _capture(tmp_path, [])
    locator.events_path.unlink()

    with pytest.raises(NativeTranscriptError) as exc_info:
        read_native_transcript(locator)

    assert exc_info.value.code == "capture_unavailable"


@pytest.mark.parametrize(
    "transcript_request",
    [
        TranscriptRequest(after_event_line=True),
        TranscriptRequest(max_messages="one"),  # type: ignore[arg-type]
        TranscriptRequest(max_content_chars=False),
        TranscriptRequest(timestamp_every_seconds="five"),  # type: ignore[arg-type]
    ],
)
def test_rejects_non_integer_request_bounds(
    tmp_path, transcript_request: TranscriptRequest
) -> None:
    with pytest.raises(NativeTranscriptError) as exc_info:
        read_native_transcript(_capture(tmp_path, []), transcript_request)

    assert exc_info.value.code == "invalid_request"


def test_renderer_marks_periodic_timestamps_and_more_messages(tmp_path) -> None:
    locator = _capture(
        tmp_path,
        [
            _event("prompt:submit", "2026-09-15T10:00:00Z", prompt="one"),
            _event("prompt:complete", "2026-09-15T10:00:30Z", response="two"),
            _event("prompt:submit", "2026-09-15T10:05:30Z", prompt="three"),
        ],
    )
    page = read_native_transcript(locator, TranscriptRequest(max_messages=2))
    all_messages = read_native_transcript(locator)

    rendered = render_native_transcript(all_messages, timestamp_every_seconds=300)

    assert rendered.count("@ 2026-09-15") == 2
    assert "[MORE MESSAGES AVAILABLE | resume with after-event-line=2]" in render_native_transcript(
        page
    )
    assert "[END OF TRANSCRIPT | 3 messages shown]" in rendered
