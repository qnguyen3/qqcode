from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.stubs.sleep_tool import SleepTool
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    BaseEvent,
    ContextUsageEvent,
    FunctionCall,
    ToolCall,
    ToolResultEvent,
)
from vibe.core.utils import ApprovalResponse


def make_tool_call(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name="sleep_tool", arguments=json.dumps({"seconds": 0.0, "label": call_id})
        ),
    )


def make_agent(approval_callback: Any, *, call_ids: list[str]) -> Agent:
    backend = FakeBackend([
        mock_llm_chunk(
            content="Working.",
            tool_calls=[make_tool_call(call_id) for call_id in call_ids],
        ),
        mock_llm_chunk(content="Done.", finish_reason="stop"),
    ])
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        enabled_tools=["sleep_tool"],
        tools={"sleep_tool": BaseToolConfig(permission=ToolPermission.ASK)},
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
    )
    agent = Agent(config, auto_approve=False, backend=backend)
    agent.tool_manager._available["sleep_tool"] = SleepTool
    agent.set_approval_callback(approval_callback)
    return agent


async def act_and_collect_events(agent: Agent, prompt: str) -> list[BaseEvent]:
    return [
        event
        async for event in agent.act(prompt)
        if not isinstance(event, ContextUsageEvent)
    ]


@pytest.mark.asyncio
async def test_approval_callbacks_run_sequentially_and_in_order() -> None:
    order: list[tuple[str, str]] = []

    async def approval_callback(
        _tool_name: str, _args: dict[str, Any], tool_call_id: str
    ) -> tuple[str, str | None]:
        order.append(("start", tool_call_id))
        await asyncio.sleep(0.05)
        order.append(("end", tool_call_id))
        return (ApprovalResponse.YES, None)

    agent = make_agent(approval_callback, call_ids=["call_1", "call_2"])

    events = await act_and_collect_events(agent, "go")

    starts = [item for item in order if item[0] == "start"]
    ends = [item for item in order if item[0] == "end"]
    assert starts == [("start", "call_1"), ("start", "call_2")]
    assert ends == [("end", "call_1"), ("end", "call_2")]
    assert order[1] == ("end", "call_1")  # no overlap between prompts

    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool_call_id for event in result_events] == ["call_1", "call_2"]
    assert agent.stats.tool_calls_agreed == 2
    assert agent.stats.tool_calls_succeeded == 2


@pytest.mark.asyncio
async def test_skipped_tool_is_not_executed_while_others_run() -> None:
    async def approval_callback(
        _tool_name: str, _args: dict[str, Any], tool_call_id: str
    ) -> tuple[str, str | None]:
        if tool_call_id == "call_1":
            return (ApprovalResponse.NO, "not this one")
        return (ApprovalResponse.YES, None)

    agent = make_agent(approval_callback, call_ids=["call_1", "call_2"])
    tool_instance = agent.tool_manager.get("sleep_tool")
    assert isinstance(tool_instance, SleepTool)

    events = await act_and_collect_events(agent, "go")

    assert tool_instance.state.invocations == ["call_2"]
    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool_call_id for event in result_events] == ["call_1", "call_2"]
    assert result_events[0].skipped is True
    assert result_events[0].skip_reason == "not this one"
    assert result_events[1].skipped is False
    assert agent.stats.tool_calls_rejected == 1
    assert agent.stats.tool_calls_agreed == 1
    assert agent.stats.tool_calls_succeeded == 1
