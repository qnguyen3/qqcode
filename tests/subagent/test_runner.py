from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.subagent.helpers import make_config, make_task_call
from vibe.core.agent import Agent
from vibe.core.config import SubagentConfig
from vibe.core.subagent import SubAgentRunner
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.tools.builtins.task import TaskArgs, TaskResult
from vibe.core.types import (
    AssistantEvent,
    ContextUsageEvent,
    FunctionCall,
    Role,
    SubagentEvent,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
)


def make_parent(tmp_path: Path, backend: FakeBackend) -> Agent:
    config = make_config(
        tmp_path, tools={"task": BaseToolConfig(permission=ToolPermission.ALWAYS)}
    )
    return Agent(config, auto_approve=True, backend=backend)


@pytest.mark.asyncio
async def test_parent_calls_task_and_receives_child_output(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="Delegating.",
            tool_calls=[make_task_call("count files", "count the files")],
        ),
        mock_llm_chunk(
            content="There are 3 files.",
            prompt_tokens=11,
            completion_tokens=4,
            finish_reason="stop",
        ),
        mock_llm_chunk(content="The subagent counted 3 files.", finish_reason="stop"),
    ])
    parent = make_parent(tmp_path, backend)

    events = [
        event
        async for event in parent.act("count the files")
        if not isinstance(event, ContextUsageEvent)
    ]

    call_events = [event for event in events if isinstance(event, ToolCallEvent)]
    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool_name for event in call_events] == ["task"]
    assert len(result_events) == 1

    result = result_events[0].result
    assert isinstance(result, TaskResult)
    assert result.success is True
    assert result.output == "There are 3 files."
    assert result.turns == 1
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 4
    assert result.error is None

    # Subagent progress events reach the parent event stream.
    subagent_events = [event for event in events if isinstance(event, SubagentEvent)]
    assert any(event.status == "running" for event in subagent_events)
    assert any(event.status == "completed" for event in subagent_events)

    # Parent history gains exactly one tool result message, nothing else.
    roles = [message.role for message in parent.messages]
    assert roles == [Role.system, Role.user, Role.assistant, Role.tool, Role.assistant]
    tool_messages = [
        message for message in parent.messages if message.role is Role.tool
    ]
    assert len(tool_messages) == 1
    assert "There are 3 files." in (tool_messages[0].content or "")

    final_event = events[-1]
    assert isinstance(final_event, AssistantEvent)
    assert final_event.content == "The subagent counted 3 files."


@pytest.mark.asyncio
async def test_task_result_formats_failure_for_model() -> None:
    ok = TaskResult(
        description="d",
        output="hello",
        turns=2,
        prompt_tokens=1,
        completion_tokens=3,
        success=True,
    )
    assert "hello" in ok.format_for_model()

    bad = TaskResult(
        description="d",
        output="",
        turns=1,
        prompt_tokens=1,
        completion_tokens=3,
        success=False,
        error="boom",
    )
    text = bad.format_for_model()
    assert "failed" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_stream_tools_forwards_child_tool_events(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ls_call = ToolCall(id="ls_1", function=FunctionCall(name="ls", arguments="{}"))
    backend = FakeBackend([
        mock_llm_chunk(content="Checking.", tool_calls=[ls_call]),
        mock_llm_chunk(content="Listed.", finish_reason="stop"),
    ])
    config = make_config(tmp_path, subagent=SubagentConfig(stream_tools=True))
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    result = await runner.run(TaskArgs(description="list files", prompt="list them"))

    assert result.success is True
    assert result.output == "Listed."

    forwarded: list[SubagentEvent | ToolCallEvent | ToolResultEvent] = []
    while not parent._subagent_event_queue.empty():
        forwarded.append(parent._subagent_event_queue.get_nowait())

    call_names = [
        event.tool_name for event in forwarded if isinstance(event, ToolCallEvent)
    ]
    result_names = [
        event.tool_name for event in forwarded if isinstance(event, ToolResultEvent)
    ]
    assert "[sub] ls" in call_names
    assert "[sub] ls" in result_names
