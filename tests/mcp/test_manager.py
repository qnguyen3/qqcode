from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest

from tests.mcp.helpers import stdio_server
from vibe.core.config import VibeConfig
from vibe.core.mcp.exposure import MCP_SEARCH_TOOL_NAME
from vibe.core.mcp.manager import MCPManager


def _config(
    server_name: str,
    tmp_path: Path,
    *,
    enabled: bool = True,
    tool_exposure: Literal["direct", "deferred", "hidden"] = "direct",
) -> VibeConfig:
    server = stdio_server(
        server_name,
        state_file=tmp_path / "state.log",
        enabled=enabled,
        tool_exposure=tool_exposure,
    )
    return VibeConfig(
        system_prompt_id="tests", include_project_context=False, mcp_servers=[server]
    )


@pytest.mark.asyncio
async def test_manager_registers_tools_and_resources(tmp_path: Path) -> None:
    manager = MCPManager(_config("fake", tmp_path))
    await manager.start()
    try:
        names = {cls.get_name() for cls in manager.cached_tool_classes()}
        assert {"fake_echo", "fake_add"} <= names
        assert {
            "mcp__fake__list_resources",
            "mcp__fake__list_resource_templates",
            "mcp__fake__read_resource",
        } <= names
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_refreshes_on_tool_list_changed(tmp_path: Path) -> None:
    manager = MCPManager(_config("fake", tmp_path))
    await manager.start()
    changes = 0

    def _listener() -> None:
        nonlocal changes
        changes += 1

    manager.on_tools_changed(_listener)
    try:
        client = manager.clients["fake"]
        await client.call_tool("trigger_change", {})

        async with asyncio.timeout(10):
            while "fake_late_tool" not in {
                cls.get_name() for cls in manager.cached_tool_classes()
            }:
                await asyncio.sleep(0.05)
        assert changes >= 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_stop_closes_clients(tmp_path: Path) -> None:
    manager = MCPManager(_config("fake", tmp_path))
    await manager.start()
    client = manager.clients["fake"]
    assert client.connected

    await manager.stop()

    assert not client.connected


@pytest.mark.asyncio
async def test_disabled_server_is_not_connected(tmp_path: Path) -> None:
    manager = MCPManager(_config("off", tmp_path, enabled=False))
    await manager.start()
    try:
        assert manager.clients == {}
        assert manager.cached_tool_classes() == []

        statuses = await manager.server_statuses()
        assert statuses[0].enabled is False
        assert statuses[0].connected is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_server_statuses_report_tool_count(tmp_path: Path) -> None:
    manager = MCPManager(_config("fake", tmp_path))
    await manager.start()
    try:
        statuses = await manager.server_statuses()
        assert statuses[0].name == "fake"
        assert statuses[0].connected is True
        assert statuses[0].tools == 4
        assert statuses[0].exposure == "direct"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_search_tool_absent_for_direct_servers(tmp_path: Path) -> None:
    manager = MCPManager(_config("fake", tmp_path))
    await manager.start()
    try:
        names = {cls.get_name() for cls in manager.cached_tool_classes()}
        assert MCP_SEARCH_TOOL_NAME not in names
    finally:
        await manager.stop()
