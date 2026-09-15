"""Context Intelligence read tools — graph_query, blob_read, whoami, and session_transcript.

All four tools share one ToolConfigResolver, so sources has a single
config namespace: overrides.tool-context-intelligence-query.config.sources.

WhoamiTool itself lives in the shared context_intelligence library
(context_intelligence/whoami_tool.py), not in this module's own package --
it is ALSO mounted by tool-server-data-ops (the delete agent's module),
which needs identity for ownership checks but must not have graph_query.
Importing the same class from the shared location keeps the two mounts
in lock-step with zero duplication. No agent mounts both this module AND
tool-server-data-ops, so there is no "whoami" name collision.

Four tools, one mount(): idiomatic multi-tool module (same as tool-filesystem
which mounts read_file / write_file / edit_file from one mount() call).
"""

from __future__ import annotations

from typing import Any

__amplifier_module_type__ = "tool"
__all__ = ["mount"]


async def mount(coordinator: Any, config: Any) -> None:
    """Mount all four CI read tools, sharing one ToolConfigResolver where needed.

    The resolver is built ONCE from the module's config and injected into
    graph_query, blob_read, and whoami. Tool constructors no longer accept config
    — the resolver IS their shared config surface.

    The hook resolver is NOT fetched here; each tool fetches it lazily at
    first execute() because tools mount before hooks (kernel phase order is
    orchestrator → context → providers → tools → hooks — CONTRACTS.md §Module
    Lifecycle Methods).  The execute-time lazy hook-resolver fetch remains
    untouched.
    """
    from context_intelligence.tool_resolver import ToolConfigResolver
    from context_intelligence.whoami_tool import WhoamiTool

    from .blob_read_tool import BlobReadTool
    from .graph_query_tool import GraphQueryTool
    from .session_transcript_tool import SessionTranscriptTool

    resolver = ToolConfigResolver(config or {}, coordinator)  # built ONCE
    # WARN-only diagnostic pass (criterion 4) -- no longer raises; hard validation is
    # now per-source at query time (see tool_resolver.py: validate_source()).
    resolver.validate_sources()
    gq = GraphQueryTool(coordinator, resolver)
    br = BlobReadTool(coordinator, resolver)
    whoami = WhoamiTool(coordinator, resolver)
    transcript = SessionTranscriptTool(coordinator)
    await coordinator.mount("tools", gq, name=gq.name)  # "graph_query"
    await coordinator.mount("tools", br, name=br.name)  # "blob_read"
    await coordinator.mount("tools", whoami, name=whoami.name)  # "whoami"
    await coordinator.mount("tools", transcript, name=transcript.name)  # "session_transcript"
