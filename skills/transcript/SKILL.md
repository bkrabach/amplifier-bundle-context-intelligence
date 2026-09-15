---
name: transcript
description: Retrieve the prior user and assistant conversation verbatim from the current Context Intelligence capture, or named sessions. Use when a user asks to replay, quote, review, or act on a transcript.
version: 1.0.0
license: MIT
user-invocable: true
compatibility: Amplifier with the session_transcript tool mounted
---

# Transcript

Use `session_transcript`; never read `events.jsonl` directly.

> **Sensitive content:** stored captures are replayed verbatim and can contain
> sensitive content. The logging hook's JSON sanitization is not redaction.

Interpret `$ARGUMENTS` as follows:

- **No arguments:** call `session_transcript` with no `session_ids`. Return the
  complete role-marked transcript pages verbatim. Do not summarize or add
  commentary.
- **Intent only:** call `session_transcript` with no `session_ids`, page through
  the whole transcript, then perform the requested intent using it as source
  material. State that the answer was based on the native transcript.
- **`--session ID[,ID...]`:** retrieve those sessions in the listed order. With
  no further intent, replay each transcript verbatim under its own session
  heading.
- **`--session ID[,ID...] -- <intent>`:** retrieve all named sessions first,
  then perform the intent using those transcripts.

For a single session, keep calling `session_transcript` with its returned
`after_event_line` while `has_more` is true. For multiple sessions, retrieve and
page one session at a time; use the returned per-session cursors when issuing a
batched follow-up. A page boundary is normal; preserve the tool's
`[MORE MESSAGES AVAILABLE ...]` marker between pages. Do not hide capture
issues, infer missing messages, use graph reconstruction, or fall back to raw
provider events.
