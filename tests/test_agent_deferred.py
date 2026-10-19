from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, ToolRuntimeConfig, VibeConfig
from vibe.core.context.tokens import estimate_tool_tokens
from vibe.core.tools.base import BaseTool, BaseToolConfig, ToolPermission
from vibe.core.tools.manager import ToolManager
from vibe.core.types import AvailableFunction, AvailableTool, FunctionCall, ToolCall


def make_config(tmp_path: Path, **runtime: object) -> VibeConfig:
    return VibeConfig(
        workdir=tmp_path,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        enabled_tools=["todo", "tool_search"],
        tools={"todo": BaseToolConfig(permission=ToolPermission.ALWAYS)},
        tool_runtime=ToolRuntimeConfig.model_validate(runtime),
    )


def tool_names(tools) -> set[str]:
    return {tool.function.name for tool in tools or []}


def available_tool(tool_class: type[BaseTool]) -> AvailableTool:
    return AvailableTool(
        function=AvailableFunction(
            name=tool_class.get_name(),
            description=tool_class.description,
            parameters=tool_class.get_parameters(),
        )
    )


def test_exposure_defaults_to_direct(tmp_path: Path) -> None:
    manager = ToolManager(make_config(tmp_path))

    assert manager.exposure_for("todo") == "direct"
    assert "todo" in {cls.get_name() for cls in manager.model_visible_tools()}
    assert manager.deferred_pool() == {}


def test_config_glob_defers_tools(tmp_path: Path) -> None:
    config = make_config(tmp_path, exposure={"tod*": "deferred", "todo": "hidden"})
    manager = ToolManager(config)

    assert manager.exposure_for("todo") == "hidden"
    assert "todo" not in {cls.get_name() for cls in manager.model_visible_tools()}


def test_deferred_tools_stay_hidden_until_exposed(tmp_path: Path) -> None:
    config = make_config(tmp_path, exposure={"todo": "deferred"})
    manager = ToolManager(config)

    assert manager.deferred_pool() == {
        "todo": manager.available_tools()["todo"].description
    }
    assert "todo" not in {cls.get_name() for cls in manager.model_visible_tools()}
    assert manager.tool_search_class() is not None

    assert manager.expose(["todo", "not_a_tool"]) == ["todo"]
    assert "todo" in {cls.get_name() for cls in manager.model_visible_tools()}

    manager.reset_deferred_exposure()
    assert "todo" not in {cls.get_name() for cls in manager.model_visible_tools()}


def test_no_deferred_tools_means_no_tool_search(tmp_path: Path) -> None:
    manager = ToolManager(make_config(tmp_path))

    assert manager.tool_search_class() is None


@pytest.mark.asyncio
async def test_tool_search_exposes_matches(tmp_path: Path) -> None:
    config = make_config(tmp_path, exposure={"todo": "deferred"})
    manager = ToolManager(config)
    search = manager.get("tool_search")

    result = await search.invoke(query="todo list")

    assert "todo" in result.exposed
    assert any(match.name == "todo" for match in result.matches)
    assert "todo" in {cls.get_name() for cls in manager.model_visible_tools()}


def test_schema_tokens_drop_when_tools_are_deferred(tmp_path: Path) -> None:
    visible = ToolManager(make_config(tmp_path))
    deferred = ToolManager(
        make_config(tmp_path, exposure={"todo": "deferred", "read_file": "deferred"})
    )
    names = {"todo", "read_file"}

    visible_tools = [
        available_tool(cls)
        for cls in visible.model_visible_tools()
        if cls.get_name() in names
    ]
    deferred_tools = [
        available_tool(cls)
        for cls in deferred.model_visible_tools()
        if cls.get_name() in names
    ]

    assert deferred_tools == []
    assert estimate_tool_tokens(deferred_tools) < estimate_tool_tokens(visible_tools)


@pytest.mark.asyncio
async def test_agent_exposes_deferred_tools_for_one_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path, exposure={"todo": "deferred"})
    backend = FakeBackend([
        mock_llm_chunk(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_search",
                    function=FunctionCall(
                        name="tool_search", arguments='{"query": "todo"}'
                    ),
                )
            ],
        ),
        mock_llm_chunk(content="Done.", finish_reason="stop"),
        mock_llm_chunk(content="Again.", finish_reason="stop"),
    ])
    agent = Agent(config, auto_approve=True, backend=backend)

    async for _ in agent.act("plan my work"):
        pass

    assert len(backend.requests_tools) == 2
    assert "todo" not in tool_names(backend.requests_tools[0])
    assert "tool_search" in tool_names(backend.requests_tools[0])
    assert "todo" in tool_names(backend.requests_tools[1])

    async for _ in agent.act("anything else"):
        pass

    assert "todo" not in tool_names(backend.requests_tools[2])
    assert agent.tool_manager.exposure_for("todo") == "deferred"
