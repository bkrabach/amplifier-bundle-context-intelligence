"""Module-level contract tests for tool-context-intelligence-transcript."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock


class TestModuleContract:
    def test_module_type_is_tool(self) -> None:
        from amplifier_module_tool_context_intelligence_transcript import __amplifier_module_type__

        assert __amplifier_module_type__ == "tool"

    def test_mount_is_coroutine(self) -> None:
        from amplifier_module_tool_context_intelligence_transcript import mount

        assert inspect.iscoroutinefunction(mount)

    async def test_mount_registers_only_session_transcript_and_returns_none(self) -> None:
        from amplifier_module_tool_context_intelligence_transcript import mount

        coordinator = MagicMock()
        coordinator.mount = AsyncMock()

        result = await mount(coordinator, config={})

        assert result is None
        coordinator.mount.assert_awaited_once()
        call = coordinator.mount.call_args
        assert call.args[0] == "tools"
        assert call.kwargs["name"] == "session_transcript"
