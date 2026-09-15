"""Strict, local reading of native Context Intelligence transcript events.

This module deliberately accepts an explicit capture location. It does not know
which host created the capture or where a host stores session directories.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

_SUPPORTED_SCHEMA_VERSION = "1.0.0"
_MAX_EVENT_BYTES = 1_000_000

TranscriptRole = Literal["user", "assistant"]
TranscriptStatus = Literal["complete", "partial"]


class NativeTranscriptError(RuntimeError):
    """A capture cannot safely produce a transcript page."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaptureLocator:
    """Explicit paths for one native Context Intelligence capture."""

    events_path: Path
    metadata_path: Path

    @classmethod
    def from_session_dir(cls, session_dir: Path | str) -> "CaptureLocator":
        directory = Path(session_dir)
        return cls(
            events_path=directory / "events.jsonl", metadata_path=directory / "metadata.json"
        )


@dataclass(frozen=True)
class TranscriptRequest:
    """Bounds and presentation choices for one transcript page."""

    after_event_line: int = 0
    max_messages: int = 50
    max_content_chars: int = 50_000
    timestamp_every_seconds: int = 300


@dataclass(frozen=True)
class TranscriptMessage:
    sequence: int
    role: TranscriptRole
    content: str
    timestamp: str
    event_line: int
    source_event: Literal["prompt:submit", "prompt:complete"]


@dataclass(frozen=True)
class TranscriptIssue:
    code: str
    event_line: int | None
    detail: str


@dataclass(frozen=True)
class TranscriptPage:
    status: TranscriptStatus
    session_id: str
    workspace: str
    schema_version: str
    messages: tuple[TranscriptMessage, ...]
    issues: tuple[TranscriptIssue, ...]
    through_event_line: int | None
    next_after_event_line: int | None
    has_more: bool
    content_chars: int

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-shaped transcript page."""
        return {
            "status": self.status,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "schema_version": self.schema_version,
            "messages": [asdict(message) for message in self.messages],
            "issues": [asdict(issue) for issue in self.issues],
            "through_event_line": self.through_event_line,
            "next_after_event_line": self.next_after_event_line,
            "has_more": self.has_more,
            "content_chars": self.content_chars,
        }


def _load_metadata(locator: CaptureLocator) -> dict[str, Any]:
    try:
        metadata = json.loads(locator.metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NativeTranscriptError(
            "metadata_unavailable", f"metadata not found: {locator.metadata_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise NativeTranscriptError(
            "metadata_unavailable", f"invalid metadata JSON: {locator.metadata_path}"
        ) from exc
    except OSError as exc:
        raise NativeTranscriptError(
            "read_error", f"could not read metadata: {locator.metadata_path}"
        ) from exc

    if not isinstance(metadata, dict):
        raise NativeTranscriptError("metadata_unavailable", "metadata must be a JSON object")
    if metadata.get("format") != "context-intelligence":
        raise NativeTranscriptError(
            "unsupported_capture_format",
            f"expected context-intelligence capture, got {metadata.get('format')!r}",
        )
    version = metadata.get("version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        raise NativeTranscriptError(
            "unsupported_schema_version",
            f"unsupported capture version {version!r}; expected {_SUPPORTED_SCHEMA_VERSION}",
        )
    session_id = metadata.get("session_id")
    workspace = metadata.get("workspace")
    if not isinstance(session_id, str) or not session_id:
        raise NativeTranscriptError("metadata_unavailable", "metadata has no session_id")
    if not isinstance(workspace, str):
        raise NativeTranscriptError("metadata_unavailable", "metadata has no workspace")
    return metadata


def _validate_request(request: TranscriptRequest) -> None:
    if (
        not isinstance(request.after_event_line, int)
        or isinstance(request.after_event_line, bool)
        or request.after_event_line < 0
    ):
        raise NativeTranscriptError("invalid_request", "after_event_line must be at least zero")
    if (
        not isinstance(request.max_messages, int)
        or isinstance(request.max_messages, bool)
        or not 1 <= request.max_messages <= 200
    ):
        raise NativeTranscriptError("invalid_request", "max_messages must be between 1 and 200")
    if (
        not isinstance(request.max_content_chars, int)
        or isinstance(request.max_content_chars, bool)
        or not 1 <= request.max_content_chars <= 200_000
    ):
        raise NativeTranscriptError(
            "invalid_request", "max_content_chars must be between 1 and 200000"
        )
    if (
        not isinstance(request.timestamp_every_seconds, int)
        or isinstance(request.timestamp_every_seconds, bool)
        or not 1 <= request.timestamp_every_seconds <= 86_400
    ):
        raise NativeTranscriptError(
            "invalid_request", "timestamp_every_seconds must be between 1 and 86400"
        )


def _iter_bounded_event_lines(events_path: Path):
    """Yield physical event lines without materialising an unbounded payload."""
    with events_path.open("rb") as event_file:
        line_number = 0
        while raw_line := event_file.readline(_MAX_EVENT_BYTES + 1):
            line_number += 1
            if len(raw_line) > _MAX_EVENT_BYTES:
                while not raw_line.endswith(b"\n"):
                    raw_line = event_file.readline(_MAX_EVENT_BYTES + 1)
                    if not raw_line:
                        yield line_number, None
                        return
                yield line_number, None
                continue
            try:
                yield line_number, raw_line.decode("utf-8")
            except UnicodeDecodeError:
                yield line_number, None


def _message_from_event(
    event: dict[str, Any], line_number: int
) -> TranscriptMessage | TranscriptIssue | None:
    event_name = event.get("event")
    if event_name not in {"prompt:submit", "prompt:complete"}:
        return None
    data = event.get("data")
    timestamp = event.get("timestamp")
    if not isinstance(data, dict) or not isinstance(timestamp, str):
        return TranscriptIssue(
            "invalid_event_envelope",
            line_number,
            "prompt event lacks object data or string timestamp",
        )

    role: TranscriptRole = "user" if event_name == "prompt:submit" else "assistant"
    field = "prompt" if role == "user" else "response"
    content = data.get(field)
    if not isinstance(content, str):
        return TranscriptIssue(
            "missing_message_field",
            line_number,
            f"{event_name} lacks string data.{field}",
        )
    return TranscriptMessage(
        sequence=0,
        role=role,
        content=content,
        timestamp=timestamp,
        event_line=line_number,
        source_event=event_name,
    )


def read_native_transcript(
    locator: CaptureLocator, request: TranscriptRequest = TranscriptRequest()
) -> TranscriptPage:
    """Read one bounded, lossless page of native user and assistant messages.

    A normal page boundary is successful pagination: the next complete message
    is left for the next call and ``has_more`` is true. A partial status means
    an actual capture problem, never merely that the requested page filled.
    """
    _validate_request(request)
    metadata = _load_metadata(locator)

    messages: list[TranscriptMessage] = []
    issues: list[TranscriptIssue] = []
    content_chars = 0
    through_event_line: int | None = request.after_event_line or None
    has_more = False

    try:
        for line_number, raw_line in _iter_bounded_event_lines(locator.events_path):
            if line_number <= request.after_event_line:
                continue
            if raw_line is None:
                issues.append(
                    TranscriptIssue(
                        "event_too_large_or_invalid_encoding",
                        line_number,
                        f"event line exceeds {_MAX_EVENT_BYTES} bytes or is not UTF-8",
                    )
                )
                through_event_line = line_number
                continue
            if line_number <= request.after_event_line or not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                issues.append(
                    TranscriptIssue("malformed_json", line_number, "invalid JSON event line")
                )
                through_event_line = line_number
                continue
            if not isinstance(event, dict):
                issues.append(
                    TranscriptIssue(
                        "invalid_event_envelope", line_number, "event line is not an object"
                    )
                )
                through_event_line = line_number
                continue
            message_or_issue = _message_from_event(event, line_number)
            if isinstance(message_or_issue, TranscriptIssue):
                issues.append(message_or_issue)
                through_event_line = line_number
                continue
            if message_or_issue is None:
                through_event_line = line_number
                continue

            next_size = content_chars + len(message_or_issue.content)
            if not messages and len(message_or_issue.content) > request.max_content_chars:
                raise NativeTranscriptError(
                    "message_too_large",
                    f"event line {line_number} contains {len(message_or_issue.content)} characters; "
                    f"increase max_content_chars above {request.max_content_chars}",
                )
            if len(messages) >= request.max_messages or next_size > request.max_content_chars:
                has_more = True
                break
            messages.append(
                TranscriptMessage(
                    sequence=len(messages) + 1,
                    role=message_or_issue.role,
                    content=message_or_issue.content,
                    timestamp=message_or_issue.timestamp,
                    event_line=message_or_issue.event_line,
                    source_event=message_or_issue.source_event,
                )
            )
            content_chars = next_size
            through_event_line = line_number
    except FileNotFoundError as exc:
        raise NativeTranscriptError(
            "capture_unavailable", f"events file not found: {locator.events_path}"
        ) from exc
    except OSError as exc:
        raise NativeTranscriptError(
            "read_error", f"could not read events: {locator.events_path}"
        ) from exc

    status: TranscriptStatus = "partial" if issues else "complete"
    next_after = through_event_line if has_more else None
    return TranscriptPage(
        status=status,
        session_id=metadata["session_id"],
        workspace=metadata["workspace"],
        schema_version=metadata["version"],
        messages=tuple(messages),
        issues=tuple(issues),
        through_event_line=through_event_line,
        next_after_event_line=next_after,
        has_more=has_more,
        content_chars=content_chars,
    )


def _timestamp_is_due(previous: str | None, current: str, interval_seconds: int) -> bool:
    """Return whether an exact source timestamp needs a new display marker."""
    if previous is None:
        return True
    try:
        before = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        after = datetime.fromisoformat(current.replace("Z", "+00:00"))
    except ValueError:
        # An invalid timestamp cannot be safely compared. Showing it preserves
        # provenance and makes the capture defect visible without invention.
        return True
    return before.date() != after.date() or (after - before).total_seconds() >= interval_seconds


def render_native_transcript(page: TranscriptPage, *, timestamp_every_seconds: int = 300) -> str:
    """Render a compact role-marked transcript without altering message content."""
    _validate_request(TranscriptRequest(timestamp_every_seconds=timestamp_every_seconds))
    lines = [
        f"=== SESSION {page.session_id} ===",
        f"Workspace: {page.workspace}",
        f"Capture: native events.jsonl (schema {page.schema_version})",
        "",
    ]
    last_marker_timestamp: str | None = None
    for message in page.messages:
        if _timestamp_is_due(last_marker_timestamp, message.timestamp, timestamp_every_seconds):
            lines.append(f"@ {message.timestamp}")
            last_marker_timestamp = message.timestamp
        lines.append(f"[{message.role.upper()} | event-line {message.event_line}]")
        lines.append(message.content)
        lines.append("")

    if page.has_more:
        lines.append(
            f"[MORE MESSAGES AVAILABLE | resume with after-event-line={page.next_after_event_line}]"
        )
    elif page.issues:
        lines.append("[END OF TRANSCRIPT WITH CAPTURE ISSUES]")
    else:
        lines.append(f"[END OF TRANSCRIPT | {len(page.messages)} messages shown]")
    return "\n".join(lines)
