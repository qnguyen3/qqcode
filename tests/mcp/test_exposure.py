from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from tests.mcp.helpers import stdio_server
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.llm.format import get_active_tool_classes
from vibe.core.mcp.exposure import MCP_SEARCH_TOOL_NAME, MCPSearchResult, bm25_scores
from vibe.core.mcp.manager import MCPManager
from vibe.core.tools.manager import ToolManager


def _config(
    tmp_path: Path, exposure: Literal["direct", "deferred", "hidden"]
) -> VibeConfig:
    server = stdio_server(
        "fake", state_file=tmp_path / "state.log", tool_exposure=exposure
    )
    return VibeConfig(
        system_prompt_id="tests", include_project_context=False, mcp_servers=[server]
    )


def _agent_config(
    tmp_path: Path, exposure: Literal["direct", "deferred", "hidden"]
) -> VibeConfig:
    server = stdio_server(
        "fake", state_file=tmp_path / "state.log", tool_exposure=exposure
    )
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        workdir=tmp_path,
        mcp_servers=[server],
    )


def _schema_names(tools: ToolManager, config: VibeConfig) -> set[str]:
    return {cls.get_name() for cls in get_active_tool_classes(tools, config)}


def test_bm25_ranks_matching_documents_higher() -> None:
    scores = bm25_scores(
        "create issue",
        {
            "create_issue": "create a github issue with a title",
            "list_files": "list files in a directory",
            "echo": "echo text back",
        },
    )

    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    assert ranked[0] == "create_issue"
    assert scores["echo"] == 0


@pytest.mark.asyncio
async def test_hidden_server_tools_are_callable_but_not_exposed(tmp_path: Path) -> None:
    config = _config(tmp_path, "hidden")
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        names = {cls.get_name() for cls in manager.cached_tool_classes()}
        assert "fake_echo" not in names
        assert MCP_SEARCH_TOOL_NAME not in names
        assert "fake_echo" not in _schema_names(tools, config)

        assert "fake_echo" in tools.available_tools()
        hidden = tools.get("fake_echo")
        result = await hidden.invoke(text="hello")
        assert result.structured == {"result": "echo:hello"}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_deferred_tools_appear_after_search(tmp_path: Path) -> None:
    config = _config(tmp_path, "deferred")
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        names = {cls.get_name() for cls in manager.cached_tool_classes()}
        assert MCP_SEARCH_TOOL_NAME in names
        assert "fake_echo" not in names
        assert "fake_echo" not in _schema_names(tools, config)

        search = tools.get(MCP_SEARCH_TOOL_NAME)
        result = await search.invoke(query="echo text")
        assert isinstance(result, MCPSearchResult)
        assert "fake_echo" in result.activated

        assert "fake_echo" in _schema_names(tools, config)
        echo_tool = tools.get("fake_echo")
        echoed = await echo_tool.invoke(text="hello")
        assert echoed.structured == {"result": "echo:hello"}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_deferred_activation_expires_on_the_next_turn(tmp_path: Path) -> None:
    config = _config(tmp_path, "deferred")
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        search = tools.get(MCP_SEARCH_TOOL_NAME)
        await search.invoke(query="echo text")
        assert "fake_echo" in _schema_names(tools, config)

        tools.reset_deferred_exposure()

        assert "fake_echo" not in _schema_names(tools, config)
        assert "fake_echo" not in {
            cls.get_name() for cls in manager.cached_tool_classes()
        }
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_agent_resets_deferred_tools_at_the_next_turn(tmp_path: Path) -> None:
    config = _agent_config(tmp_path, "deferred")
    manager = MCPManager(config)
    await manager.start()
    backend = FakeBackend([mock_llm_chunk(content="ok", finish_reason="stop")])
    agent = Agent(config, auto_approve=True, backend=backend, mcp_manager=manager)
    try:
        search = agent.tool_manager.get(MCP_SEARCH_TOOL_NAME)
        await search.invoke(query="echo text")
        assert "fake_echo" in _schema_names(agent.tool_manager, config)

        async for _ in agent.act("hi"):
            pass

        assert "fake_echo" not in _schema_names(agent.tool_manager, config)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_deferred_search_without_match_keeps_tools_hidden(tmp_path: Path) -> None:
    config = _config(tmp_path, "deferred")
    manager = MCPManager(config)
    await manager.start()
    tools = ToolManager(config, mcp_manager=manager)
    try:
        search = tools.get(MCP_SEARCH_TOOL_NAME)
        result = await search.invoke(query="zzzznothing")
        assert result.activated == []
        assert "fake_echo" not in tools.available_tools()
    finally:
        await manager.stop()
