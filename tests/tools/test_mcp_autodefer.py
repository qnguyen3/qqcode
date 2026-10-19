from __future__ import annotations

from pathlib import Path

import pytest

from tests.mcp.helpers import stdio_server
from vibe.core.config import SessionLoggingConfig, ToolRuntimeConfig, VibeConfig
from vibe.core.mcp.manager import MCPManager
from vibe.core.tools.manager import ToolManager


def make_config(tmp_path: Path, *, defer_when_more_than: int) -> VibeConfig:
    server = stdio_server("fake", state_file=tmp_path / "state.log")
    return VibeConfig(
        workdir=tmp_path,
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        mcp_servers=[server],
        tool_runtime=ToolRuntimeConfig(defer_when_more_than=defer_when_more_than),
    )


@pytest.mark.asyncio
async def test_large_mcp_tool_sets_are_deferred(tmp_path: Path) -> None:
    config = make_config(tmp_path, defer_when_more_than=1)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        assert tools.exposure_for("fake_echo") == "deferred"
        assert "fake_echo" in tools.deferred_pool()
        assert "fake_echo" not in {
            cls.get_name() for cls in tools.model_visible_tools()
        }
        assert tools.tool_search_class() is not None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_small_mcp_tool_sets_stay_direct(tmp_path: Path) -> None:
    config = make_config(tmp_path, defer_when_more_than=100)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        assert tools.exposure_for("fake_echo") == "direct"
        assert tools.deferred_pool() == {}
        assert "fake_echo" in {cls.get_name() for cls in tools.model_visible_tools()}
        assert tools.tool_search_class() is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_explicit_exposure_overrides_auto_defer(tmp_path: Path) -> None:
    config = make_config(tmp_path, defer_when_more_than=1)
    config.tool_runtime.exposure = {"fake_echo": "direct"}
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        assert tools.exposure_for("fake_echo") == "direct"
        assert "fake_echo" in {cls.get_name() for cls in tools.model_visible_tools()}
    finally:
        await manager.stop()
