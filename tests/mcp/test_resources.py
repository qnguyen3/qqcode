from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.mcp.helpers import stdio_server
from vibe.core.config import VibeConfig
from vibe.core.mcp.manager import MCPManager
from vibe.core.mcp.resources import (
    MCPReadResourceResult,
    MCPResourceListResult,
    MCPResourceTemplateListResult,
)
from vibe.core.tools.base import ToolPermission
from vibe.core.tools.manager import ToolManager


def _config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        system_prompt_id="tests",
        include_project_context=False,
        mcp_servers=[stdio_server("fake", state_file=tmp_path / "state.log")],
    )


@pytest.mark.asyncio
async def test_list_resources(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        tool = tools.get("mcp__fake__list_resources")
        result = await tool.invoke()
        assert isinstance(result, MCPResourceListResult)
        uris = {resource.uri for resource in result.resources}
        assert "resource://greeting" in uris
        assert "resource://blob" in uris
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_list_resource_templates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        tool = tools.get("mcp__fake__list_resource_templates")
        result = await tool.invoke()
        assert isinstance(result, MCPResourceTemplateListResult)
        assert any(
            template.uri_template == "resource://items/{item_id}"
            for template in result.templates
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_read_text_resource(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        tool = tools.get("mcp__fake__read_resource")
        result = await tool.invoke(uri="resource://greeting")
        assert isinstance(result, MCPReadResourceResult)
        assert result.text == "hello from fake mcp"
        assert result.blob_path is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_binary_resource_spills_to_temp_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        tool = tools.get("mcp__fake__read_resource")
        result = await tool.invoke(uri="resource://blob")
        assert result.blob_path is not None
        blob = Path(result.blob_path)
        assert blob.is_file()
        assert blob.read_bytes() == b"\x00\x01\x02\xffbinary"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_large_resource_is_truncated_with_full_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        tool = tools.get("mcp__fake__read_resource")
        result = await tool.invoke(uri="resource://big")
        assert result.truncated is True
        assert result.text is not None
        assert "[Showing lines 1-2000 of 5000" in result.text
        match = re.search(r"Full output: (\S+?)\]", result.text)
        assert match is not None
        assert Path(match.group(1)).is_file()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_resource_tools_default_to_ask_permission(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        for name in (
            "mcp__fake__list_resources",
            "mcp__fake__list_resource_templates",
            "mcp__fake__read_resource",
        ):
            assert tools.get_tool_config(name).permission == ToolPermission.ASK
    finally:
        await manager.stop()
