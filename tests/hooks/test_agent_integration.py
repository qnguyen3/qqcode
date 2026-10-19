from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.hooks.api import deny, inject, modify
from vibe.core.hooks.events import (
    AgentStartEvent,
    HookDecision,
    ToolCallEventHook,
    ToolResultEventHook,
)
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    ContextUsageEvent,
    FunctionCall,
    LLMMessage,
    Role,
    ToolCall,
    ToolResultEvent,
)


async def act_and_collect(agent: Agent, prompt: str) -> list[BaseEvent]:
    return [
        event
        async for event in agent.act(prompt)
        if not isinstance(event, ContextUsageEvent)
    ]


def make_agent(
    tmp_path: Path,
    backend: FakeBackend,
    *,
    enabled_tools: tuple[str, ...] = ("bash",),
    enable_streaming: bool = False,
) -> Agent:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        enabled_tools=list(enabled_tools),
        tools={
            name: BaseToolConfig(permission=ToolPermission.ALWAYS)
            for name in enabled_tools
        },
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        workdir=tmp_path,
    )
    return Agent(
        config, auto_approve=True, backend=backend, enable_streaming=enable_streaming
    )


def bash_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name="bash", arguments=json.dumps({"command": command})),
    )


def bash_backend(*tool_calls: ToolCall, final: str = "Done.") -> FakeBackend:
    return FakeBackend([
        mock_llm_chunk(content="Working.", tool_calls=list(tool_calls)),
        mock_llm_chunk(content=final, finish_reason="stop"),
    ])


def tool_result(events: list[BaseEvent]) -> ToolResultEvent:
    return next(event for event in events if isinstance(event, ToolResultEvent))


@pytest.mark.asyncio
async def test_hook_blocks_dangerous_bash_command(tmp_path: Path) -> None:
    canary = tmp_path / "must_not_exist"
    backend = bash_backend(bash_call("call_danger", f"touch {canary}"))
    agent = make_agent(tmp_path, backend)

    def guard(event: ToolCallEventHook) -> HookDecision | None:
        if event.tool_name == "bash" and "touch" in event.args.get("command", ""):
            return deny("destructive command blocked")
        return None

    agent.hooks.register("tool_call", guard)

    events = await act_and_collect(agent, "clean up")
    result = tool_result(events)

    assert result.skipped is True
    assert result.skip_reason == "destructive command blocked"
    assert not canary.exists()


@pytest.mark.asyncio
async def test_hook_modifies_tool_args_before_invoke(tmp_path: Path) -> None:
    original = tmp_path / "original"
    output = tmp_path / "output.txt"
    backend = bash_backend(bash_call("call_modify", f"touch {original}"))
    agent = make_agent(tmp_path, backend)
    seen_args: list[dict[str, Any]] = []

    def guard(event: ToolCallEventHook) -> HookDecision:
        seen_args.append(dict(event.args))
        return modify(replacement_args={"command": f"echo modified > {output}"})

    agent.hooks.register("tool_call", guard)

    events = await act_and_collect(agent, "go")
    result = tool_result(events)

    assert result.error is None
    assert result.skipped is False
    assert seen_args[0]["command"] == f"touch {original}"
    assert output.read_text(encoding="utf-8").strip() == "modified"
    assert not original.exists()


@pytest.mark.asyncio
async def test_hook_invalid_replacement_args_fail_the_call(tmp_path: Path) -> None:
    backend = bash_backend(bash_call("call_invalid", "echo hi"), final="Failed.")
    agent = make_agent(tmp_path, backend)
    agent.hooks.register(
        "tool_call", lambda event: modify(replacement_args={"not_a_field": True})
    )

    events = await act_and_collect(agent, "go")
    result = tool_result(events)

    assert result.error is not None
    assert "Hook replacement args invalid" in result.error


@pytest.mark.asyncio
async def test_before_request_injection_reaches_backend_but_is_transient(
    tmp_path: Path,
) -> None:
    backend = FakeBackend([mock_llm_chunk(content="hello", finish_reason="stop")])
    agent = make_agent(tmp_path, backend, enabled_tools=("todo",))
    agent.hooks.register("before_request", lambda event: inject("STYLE_MARKER"))

    await act_and_collect(agent, "hi")

    request = backend.requests_messages[0]
    assert any("STYLE_MARKER" in (message.content or "") for message in request)
    assert not any(
        "STYLE_MARKER" in (message.content or "") for message in agent.messages
    )


@pytest.mark.asyncio
async def test_before_request_deny_stops_turn_without_calling_backend(
    tmp_path: Path,
) -> None:
    backend = FakeBackend([mock_llm_chunk(content="unreachable", finish_reason="stop")])
    agent = make_agent(tmp_path, backend, enabled_tools=("todo",))
    agent.hooks.register("before_request", lambda event: deny("request rejected"))

    events = await act_and_collect(agent, "hi")

    assert backend.requests_messages == []
    assistant = next(event for event in events if isinstance(event, AssistantEvent))
    assert "request rejected" in assistant.content
    assert agent.messages[-1].role == Role.assistant


@pytest.mark.asyncio
async def test_agent_start_injects_messages_into_conversation(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="hello", finish_reason="stop")])
    agent = make_agent(tmp_path, backend, enabled_tools=("todo",))
    seen_events: list[AgentStartEvent] = []

    def on_start(event: AgentStartEvent) -> HookDecision:
        seen_events.append(event)
        return modify(
            injected_messages=[LLMMessage(role=Role.user, content="AGENT_START_MARKER")]
        )

    agent.hooks.register("agent_start", on_start)

    await act_and_collect(agent, "hi")

    assert seen_events[0].prompt == "hi"
    assert any("AGENT_START_MARKER" in (m.content or "") for m in agent.messages)
    request = backend.requests_messages[0]
    assert any("AGENT_START_MARKER" in (m.content or "") for m in request)


@pytest.mark.asyncio
async def test_tool_result_hook_observes_success_and_failure(tmp_path: Path) -> None:
    backend = bash_backend(
        bash_call("call_ok", "echo ok"), bash_call("call_bad", "exit 3")
    )
    agent = make_agent(tmp_path, backend)
    observed: list[ToolResultEventHook] = []

    def observe(event: ToolResultEventHook) -> None:
        observed.append(event)

    agent.hooks.register("tool_result", observe)

    events = await act_and_collect(agent, "go")

    assert len(observed) == 2
    by_id = {event.tool_call_id: event for event in observed}
    assert by_id["call_ok"].ok is True
    assert "ok" in (by_id["call_ok"].text or "")
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(results) == 2


@pytest.mark.asyncio
async def test_after_response_and_agent_end_are_observed(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="hi", finish_reason="stop")])
    agent = make_agent(tmp_path, backend, enabled_tools=("todo",))
    seen: list[str] = []
    agent.hooks.register("after_response", lambda event: seen.append(event.type))
    agent.hooks.register("agent_end", lambda event: seen.append(event.type))

    await act_and_collect(agent, "hi")

    assert seen == ["after_response", "agent_end"]


@pytest.mark.asyncio
async def test_broken_hook_does_not_break_agent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    backend = bash_backend(bash_call("call_1", "echo fine"))
    agent = make_agent(tmp_path, backend)

    def broken(event: ToolCallEventHook) -> HookDecision:
        raise RuntimeError("hook exploded")

    agent.hooks.register("tool_call", broken)

    with caplog.at_level(logging.WARNING, logger="vibe"):
        events = await act_and_collect(agent, "go")

    result = tool_result(events)
    assert result.error is None
    assert result.result is not None
    assert any("hook exploded" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_before_request_injection_with_streaming(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="streamed", finish_reason="stop")])
    agent = make_agent(
        tmp_path, backend, enabled_tools=("todo",), enable_streaming=True
    )
    agent.hooks.register("before_request", lambda event: inject("STREAM_MARKER"))

    await act_and_collect(agent, "hi")

    request = backend.requests_messages[0]
    assert any("STREAM_MARKER" in (message.content or "") for message in request)
