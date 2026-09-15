# Context Intelligence transcript tool

This Amplifier tool module mounts only `session_transcript`, which reads bounded,
role-marked user and assistant messages from a native Context Intelligence capture.
It does not mount graph or blob tools.

Stored captures are replayed verbatim and can contain sensitive content. The logging
hook's JSON sanitization is not redaction.