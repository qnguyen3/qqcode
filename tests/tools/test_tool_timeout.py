from __future__ import annotations

import time

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.stubs.sleep_tool import SleepTool
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    ContextUsageEvent,
    FunctionCall,
    ToolCall,
    ToolResultEvent,
)


def make_config(timeout: float | None) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        enabled_tools=["sleep_tool"],
        tools={
            "sleep_tool": BaseToolConfig(
                permission=ToolPermission.ALWAYS, timeout=timeout
            )
        },
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
    )


def make_agent(*, timeout: float | None, seconds: float) -> Agent:
    tool_call = ToolCall(
        id="call_sleep",
        function=FunctionCall(
            name="sleep_tool", arguments=f'{{"seconds": {seconds}, "label": "slept"}}'
        ),
    )
    backend = FakeBackend([
        mock_llm_chunk(content="Working.", tool_calls=[tool_call]),
        mock_llm_chunk(content="Recovered.", finish_reason="stop"),
    ])
    agent = Agent(make_config(timeout), auto_approve=True, backend=backend)
    agent.tool_manager._available["sleep_tool"] = SleepTool
    return agent


async def act_and_collect_events(agent: Agent, prompt: str) -> list[BaseEvent]:
    return [
        event
        async for event in agent.act(prompt)
        if not isinstance(event, ContextUsageEvent)
    ]


@pytest.mark.asyncio
async def test_hanging_tool_times_out_and_loop_continues() -> None:
    agent = make_agent(timeout=0.2, seconds=5.0)

    start = time.perf_counter()
    events = await act_and_collect_events(agent, "go")
    elapsed = time.perf_counter() - start

    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].error is not None
    assert "Tool timed out after 0.2s" in result_events[0].error
    assert result_events[0].result is None
    assert elapsed < 2.0
    assert agent.stats.tool_calls_failed == 1
    assert agent.stats.tool_calls_succeeded == 0
    assert isinstance(events[-1], AssistantEvent)
    assert events[-1].content == "Recovered."


@pytest.mark.asyncio
async def test_tool_config_without_timeout_still_runs() -> None:
    agent = make_agent(timeout=None, seconds=0.01)

    events = await act_and_collect_events(agent, "go")

    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].error is None
    assert result_events[0].result is not None
    assert agent.stats.tool_calls_succeeded == 1
    assert agent.stats.tool_calls_failed == 0


@pytest.mark.asyncio
async def test_zero_timeout_disables_the_timeout() -> None:
    agent = make_agent(timeout=0.0, seconds=0.01)

    events = await act_and_collect_events(agent, "go")

    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].error is None
    assert agent.stats.tool_calls_succeeded == 1


def test_tool_manager_keeps_tool_specific_defaults_when_merging() -> None:
    from vibe.core.tools.manager import ToolManager

    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        tools={"bash": BaseToolConfig(permission=ToolPermission.ALWAYS)},
    )
    manager = ToolManager(config)

    bash_config = manager.get_tool_config("bash")

    assert bash_config.permission == ToolPermission.ALWAYS
    assert bash_config.timeout == 120.0  # type: ignore[attr-defined]
    assert bash_config.default_timeout == 30  # type: ignore[attr-defined]
