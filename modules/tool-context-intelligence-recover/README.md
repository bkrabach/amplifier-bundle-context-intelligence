# context-intelligence-recover

`context-intelligence-recover` is a dedicated, durable recovery command for
native Context Intelligence captures. It reads only:

```text
<root>/<project>/sessions/<session>/context-intelligence/events.jsonl
```

and the adjacent native metadata. It never uses the ordinary event endpoint.

## Install

```bash
uv tool install "amplifier-module-tool-context-intelligence-recover @ git+https://github.com/microsoft/amplifier-bundle-context-intelligence@main#subdirectory=modules/tool-context-intelligence-recover"
```

## Use

```bash
context-intelligence-recover --once
context-intelligence-recover --path ~/saved-captures --once
context-intelligence-recover --watch
context-intelligence-recover --dry-run
```

There are intentionally no server URL or credential flags. The command resolves
the effective hook configuration for each recorded working directory by merging
the user-level settings with that project's local settings. It applies the
hook's include/exclude matcher to the recorded working directory and fans
eligible records out to every matching destination. If that effective
configuration cannot be resolved safely, the artifact remains blocked. It first
requires the target's `native-recovery` version
capability; a target without it remains blocked and is never sent through an
ordinary event endpoint.

By default, checkpoints are private SQLite files under
`~/.local/state/context-intelligence/recovery/<job-id>/`. Use `--job-id` to
resume the same job or `--state-dir` to place the checkpoint elsewhere.
`--once` returns nonzero whenever deferred, blocked, or quarantined work
remains. `--watch` honors `Retry-After` before retrying and re-reads
configuration on each cycle.

`--dry-run` (also `--plan`) inventories and evaluates routing without issuing
any HTTP requests. Source artifacts are never modified or deleted.