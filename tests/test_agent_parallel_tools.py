from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    ToolExecutionMode,
)
from vibe.core.tools.builtins.edit import Edit
from vibe.core.tools.builtins.plan_mode import ExitPlanMode, SubmitPlan
from vibe.core.types import BaseEvent, FunctionCall, Role, ToolCall, ToolResultEvent


class ConcurrencyTracker:
    def __init__(self, expected: int = 0, timeout: float = 2.0) -> None:
        self.expected = expected
        self.timeout = timeout
        self.active = 0
        self.started = 0
        self.max_active = 0
        self._gate = asyncio.Event()

    async def enter(self) -> None:
        self.active += 1
        self.started += 1
        self.max_active = max(self.max_active, self.active)
        if self.expected:
            if self.started >= self.expected:
                self._gate.set()
            await asyncio.wait_for(self._gate.wait(), timeout=self.timeout)

    def exit(self) -> None:
        self.active -= 1


class SleepArgs(BaseModel):
    duration: float = 0.05


class SleepResult(BaseModel):
    slept: float


class SleepToolState(BaseToolState):
    pass


class SleepToolBase(BaseTool[SleepArgs, SleepResult, BaseToolConfig, SleepToolState]):
    tracker: ClassVar[ConcurrencyTracker] = ConcurrencyTracker()

    async def run(self, args: SleepArgs) -> SleepResult:
        await self.tracker.enter()
        try:
            await asyncio.sleep(args.duration)
        finally:
            self.tracker.exit()
        return SleepResult(slept=args.duration)


class SleepToolA(SleepToolBase):
    @classmethod
    def get_name(cls) -> str:
        return "sleep_a"


class SleepToolB(SleepToolBase):
    @classmethod
    def get_name(cls) -> str:
        return "sleep_b"


class SleepToolC(SleepToolBase):
    @classmethod
    def get_name(cls) -> str:
        return "sleep_c"


class SequentialSleepTool(SleepToolBase):
    execution_mode: ClassVar[ToolExecutionMode] = ToolExecutionMode.SEQUENTIAL

    @classmethod
    def get_name(cls) -> str:
        return "sequential_sleep_tool"


class CancellingArgs(BaseModel):
    pass


class CancellingTool(
    BaseTool[CancellingArgs, SleepResult, BaseToolConfig, SleepToolState]
):
    @classmethod
    def get_name(cls) -> str:
        return "cancelling_tool"

    async def run(self, args: CancellingArgs) -> SleepResult:
        raise asyncio.CancelledError()


def make_config(
    enabled_tools: list[str],
    tool_execution: ToolExecutionMode = ToolExecutionMode.PARALLEL,
    max_parallel_tools: int = 4,
) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        enabled_tools=enabled_tools,
        tool_execution=tool_execution,
        max_parallel_tools=max_parallel_tools,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
    )


def sleep_call(call_id: str, tool_name: str, duration: float = 0.05) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name=tool_name, arguments=json.dumps({"duration": duration})
        ),
    )


async def act_and_collect_events(agent: Agent, prompt: str) -> list[BaseEvent]:
    return [event async for event in agent.act(prompt)]


def make_agent(
    backend: FakeBackend,
    enabled_tools: list[str],
    tool_execution: ToolExecutionMode = ToolExecutionMode.PARALLEL,
    max_parallel_tools: int = 4,
) -> Agent:
    return Agent(
        make_config(enabled_tools, tool_execution, max_parallel_tools),
        auto_approve=True,
        backend=backend,
    )


def test_tool_execution_modes() -> None:
    assert Edit.execution_mode is ToolExecutionMode.PARALLEL
    assert SubmitPlan.execution_mode is ToolExecutionMode.SEQUENTIAL
    assert ExitPlanMode.execution_mode is ToolExecutionMode.SEQUENTIAL


@pytest.mark.asyncio
async def test_parallel_tools_run_concurrently() -> None:
    tracker = ConcurrencyTracker(expected=3)
    SleepToolBase.tracker = tracker
    calls = [
        sleep_call("call_a", "sleep_a"),
        sleep_call("call_b", "sleep_b"),
        sleep_call("call_c", "sleep_c"),
    ]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Running tools", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["sleep_a", "sleep_b", "sleep_c"],
    )
    agent.tool_manager._available["sleep_a"] = SleepToolA
    agent.tool_manager._available["sleep_b"] = SleepToolB
    agent.tool_manager._available["sleep_c"] = SleepToolC

    events = await act_and_collect_events(agent, "Run three tools")

    assert tracker.max_active == 3
    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [e.tool_call_id for e in result_events] == ["call_a", "call_b", "call_c"]
    assert all(e.error is None for e in result_events)


@pytest.mark.asyncio
async def test_parallel_batch_is_bounded_by_max_parallel_tools() -> None:
    tracker = ConcurrencyTracker()
    SleepToolBase.tracker = tracker
    calls = [
        sleep_call("call_a", "sleep_a", duration=0.05),
        sleep_call("call_b", "sleep_b", duration=0.05),
        sleep_call("call_c", "sleep_c", duration=0.05),
    ]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Running tools", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["sleep_a", "sleep_b", "sleep_c"],
        max_parallel_tools=1,
    )
    agent.tool_manager._available["sleep_a"] = SleepToolA
    agent.tool_manager._available["sleep_b"] = SleepToolB
    agent.tool_manager._available["sleep_c"] = SleepToolC

    await act_and_collect_events(agent, "Run three tools")

    assert tracker.max_active == 1


@pytest.mark.asyncio
async def test_same_file_edits_serialize(tmp_path: Path) -> None:
    file_path = tmp_path / "parallel_edits_test.txt"
    file_path.write_text("a\n", encoding="utf-8")
    calls = [
        ToolCall(
            id="call_edit_1",
            function=FunctionCall(
                name="edit",
                arguments=json.dumps({
                    "file_path": str(file_path),
                    "old_string": "a",
                    "new_string": "b",
                }),
            ),
        ),
        ToolCall(
            id="call_edit_2",
            function=FunctionCall(
                name="edit",
                arguments=json.dumps({
                    "file_path": str(file_path),
                    "old_string": "b",
                    "new_string": "c",
                }),
            ),
        ),
    ]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Editing twice", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["edit"],
        max_parallel_tools=4,
    )

    events = await act_and_collect_events(agent, "Edit the file twice")

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [e.error for e in result_events] == [None, None]
    assert file_path.read_text(encoding="utf-8") == "c\n"
    assert agent.stats.tool_calls_succeeded == 2


@pytest.mark.asyncio
async def test_sequential_tool_forces_whole_batch_sequential() -> None:
    parallel_tracker = ConcurrencyTracker()
    sequential_tracker = ConcurrencyTracker()
    SleepToolA.tracker = parallel_tracker
    SequentialSleepTool.tracker = sequential_tracker
    calls = [
        sleep_call("call_parallel", "sleep_a"),
        sleep_call("call_sequential", "sequential_sleep_tool"),
    ]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Running tools", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["sleep_a", "sequential_sleep_tool"],
    )
    agent.tool_manager._available["sleep_a"] = SleepToolA
    agent.tool_manager._available["sequential_sleep_tool"] = SequentialSleepTool

    await act_and_collect_events(agent, "Run both tools")

    assert parallel_tracker.max_active == 1
    assert sequential_tracker.max_active == 1


@pytest.mark.asyncio
async def test_config_sequential_restores_sequential_execution() -> None:
    tracker = ConcurrencyTracker()
    SleepToolBase.tracker = tracker
    calls = [sleep_call("call_1", "sleep_a"), sleep_call("call_2", "sleep_b")]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Running tools", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["sleep_a", "sleep_b"],
        tool_execution=ToolExecutionMode.SEQUENTIAL,
    )
    agent.tool_manager._available["sleep_a"] = SleepToolA
    agent.tool_manager._available["sleep_b"] = SleepToolB

    await act_and_collect_events(agent, "Run two tools")

    assert tracker.max_active == 1


@pytest.mark.asyncio
async def test_cancellation_mid_batch_keeps_tool_messages() -> None:
    SleepToolBase.tracker = ConcurrencyTracker()
    calls = [
        sleep_call("call_cancel", "cancelling_tool"),
        sleep_call("call_slow", "sleep_a", duration=0.2),
    ]
    agent = make_agent(
        FakeBackend([
            mock_llm_chunk(content="Running tools", tool_calls=calls),
            mock_llm_chunk(content="Done", finish_reason="stop"),
        ]),
        enabled_tools=["cancelling_tool", "sleep_a"],
    )
    agent.tool_manager._available["cancelling_tool"] = CancellingTool
    agent.tool_manager._available["sleep_a"] = SleepToolA

    with pytest.raises(asyncio.CancelledError):
        async for _ in agent.act("Run both tools"):
            pass

    tool_messages = [m for m in agent.messages if m.role == Role.tool]
    assert {m.tool_call_id for m in tool_messages} == {"call_cancel", "call_slow"}
    assert any(
        "execution interrupted by user" in (m.content or "").lower()
        for m in tool_messages
    )
