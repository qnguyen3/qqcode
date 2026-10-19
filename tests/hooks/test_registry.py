from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from vibe.core.hooks.api import deny, inject, modify
from vibe.core.hooks.events import (
    AgentEndEvent,
    BeforeRequestEvent,
    HookDecision,
    ToolCallEventHook,
)
from vibe.core.hooks.registry import HookRegistry
from vibe.core.types import LLMUsage


def make_tool_event() -> ToolCallEventHook:
    return ToolCallEventHook(
        tool_name="bash", tool_call_id="call_1", args={"command": "ls"}
    )


@pytest.mark.asyncio
async def test_empty_registry_returns_allow_decision() -> None:
    registry = HookRegistry()

    decision = await registry.dispatch(make_tool_event())

    assert decision == HookDecision()
    assert decision.action == "allow"
    assert registry.registered_types() == []


@pytest.mark.asyncio
async def test_handlers_run_by_descending_priority_then_registration_order() -> None:
    registry = HookRegistry()
    order: list[str] = []

    def low(event: ToolCallEventHook) -> None:
        order.append("low")

    def high(event: ToolCallEventHook) -> None:
        order.append("high")

    def high_second(event: ToolCallEventHook) -> None:
        order.append("high_second")

    registry.register("tool_call", low, priority=0)
    registry.register("tool_call", high, priority=10)
    registry.register("tool_call", high_second, priority=10)

    await registry.dispatch(make_tool_event())

    assert order == ["high", "high_second", "low"]


@pytest.mark.asyncio
async def test_deny_wins_over_allow() -> None:
    registry = HookRegistry()
    registry.register("tool_call", lambda event: None)
    registry.register("tool_call", lambda event: deny("blocked"))

    decision = await registry.dispatch(make_tool_event())

    assert decision.action == "deny"
    assert decision.reason == "blocked"


@pytest.mark.asyncio
async def test_deny_wins_over_modify() -> None:
    registry = HookRegistry()
    registry.register("tool_call", lambda event: modify(replacement_args={"a": 1}))
    registry.register("tool_call", lambda event: deny("blocked"))

    decision = await registry.dispatch(make_tool_event())

    assert decision.action == "deny"
    assert decision.replacement_args == {"a": 1}


@pytest.mark.asyncio
async def test_modify_replacement_args_merge_with_later_handler_winning() -> None:
    registry = HookRegistry()
    registry.register(
        "tool_call",
        lambda event: modify(replacement_args={"a": 1, "b": 2}),
        priority=10,
    )
    registry.register(
        "tool_call", lambda event: modify(replacement_args={"b": 3, "c": 4}), priority=0
    )

    decision = await registry.dispatch(make_tool_event())

    assert decision.action == "modify"
    assert decision.replacement_args == {"a": 1, "b": 3, "c": 4}


@pytest.mark.asyncio
async def test_injected_messages_accumulate() -> None:
    registry = HookRegistry()
    registry.register("before_request", lambda event: inject("first"))
    registry.register("before_request", lambda event: inject("second"))

    event = BeforeRequestEvent(messages=[], model_alias="test-model")
    decision = await registry.dispatch(event)

    contents = [message.content for message in decision.injected_messages]
    assert contents == ["first", "second"]


@pytest.mark.asyncio
async def test_async_handler_is_supported() -> None:
    registry = HookRegistry()

    async def handler(event: Any) -> HookDecision:
        await asyncio.sleep(0)
        return deny("async denied")

    registry.register("tool_call", handler)

    decision = await registry.dispatch(make_tool_event())

    assert decision.action == "deny"
    assert decision.reason == "async denied"


@pytest.mark.asyncio
async def test_handler_error_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    registry = HookRegistry()

    def broken(event: Any) -> HookDecision:
        raise RuntimeError("boom")

    registry.register("tool_call", broken)
    registry.register("tool_call", lambda event: deny("still works"))

    with caplog.at_level(logging.WARNING, logger="vibe"):
        decision = await registry.dispatch(make_tool_event())

    assert decision.action == "deny"
    assert decision.reason == "still works"
    assert any("boom" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_slow_sync_handler_times_out_and_is_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = HookRegistry(timeout=0.05)

    def slow(event: Any) -> HookDecision:
        time.sleep(0.4)
        return deny("too late")

    registry.register("tool_call", slow)
    registry.register("tool_call", lambda event: deny("fast"))

    with caplog.at_level(logging.WARNING, logger="vibe"):
        decision = await registry.dispatch(make_tool_event())

    assert decision.action == "deny"
    assert decision.reason == "fast"
    assert any("timed out" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_slow_async_handler_times_out(caplog: pytest.LogCaptureFixture) -> None:
    registry = HookRegistry(timeout=0.05)

    async def slow(event: Any) -> HookDecision:
        await asyncio.sleep(0.4)
        return deny("too late")

    registry.register("tool_call", slow)

    with caplog.at_level(logging.WARNING, logger="vibe"):
        decision = await registry.dispatch(make_tool_event())

    assert decision.action == "allow"
    assert any("timed out" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_fire_and_forget_events_run_handlers_but_return_allow() -> None:
    registry = HookRegistry()
    calls: list[str] = []

    def handler(event: AgentEndEvent) -> HookDecision:
        calls.append(event.type)
        return deny("ignored for observe-only events")

    registry.register("agent_end", handler)

    decision = await registry.dispatch(AgentEndEvent(session_id="s1", usage=LLMUsage()))

    assert calls == ["agent_end"]
    assert decision.action == "allow"


@pytest.mark.asyncio
async def test_unrelated_event_types_do_not_run_handlers() -> None:
    registry = HookRegistry()
    calls: list[str] = []
    registry.register("tool_call", lambda event: calls.append("tool"))

    event = AgentEndEvent(session_id="s1", usage=LLMUsage())
    decision = await registry.dispatch(event)

    assert decision.action == "allow"
    assert calls == []
