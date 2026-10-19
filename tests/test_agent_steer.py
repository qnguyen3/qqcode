from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    ModelConfig,
    SessionLoggingConfig,
    ThinkingLevel,
    VibeConfig,
)
from vibe.core.llm.cache import CachePlan
from vibe.core.types import (
    AgentMode,
    AvailableTool,
    BaseEvent,
    LLMChunk,
    LLMMessage,
    StrToolChoice,
)


def make_config() -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
    )


def collect(agent: Agent, prompt: str) -> list[BaseEvent]:
    async def scenario() -> list[BaseEvent]:
        events: list[BaseEvent] = []
        async for event in agent.act(prompt):
            events.append(event)
            if len(events) == 1:
                agent.steer("also cover the edge cases")
        return events

    return asyncio.run(scenario())


def test_steer_appends_a_user_message_and_continues() -> None:
    from vibe.core.types import AssistantEvent, Role

    backend = FakeBackend([
        mock_llm_chunk(content="First answer", finish_reason="stop"),
        mock_llm_chunk(content="Second answer", finish_reason="stop"),
    ])
    agent = Agent(make_config(), mode=AgentMode.AUTO_APPROVE, backend=backend)

    events = collect(agent, "hello")

    contents = [event.content for event in events if isinstance(event, AssistantEvent)]
    assert contents == ["First answer", "Second answer"]
    assert len(backend.requests_messages) == 2

    second_request = backend.requests_messages[1]
    user_messages = [
        message.content for message in second_request if message.role is Role.user
    ]
    assert user_messages[-1] == "also cover the edge cases"


def test_steer_with_blank_text_is_ignored() -> None:
    agent = Agent(
        make_config(),
        mode=AgentMode.AUTO_APPROVE,
        backend=FakeBackend([mock_llm_chunk(content="done", finish_reason="stop")]),
    )

    agent.steer("   ")

    assert agent._steering.empty()


class HangingBackend:
    async def __aenter__(self) -> HangingBackend:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> LLMChunk:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def complete_streaming(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> AsyncGenerator[LLMChunk, None]:
        async def generator() -> AsyncGenerator[LLMChunk, None]:
            await asyncio.Event().wait()
            yield mock_llm_chunk()

        return generator()

    async def count_tokens(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        tools: list[AvailableTool] | None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None,
    ) -> int:
        return 1


def test_abort_cancels_the_running_turn() -> None:
    async def scenario() -> None:
        agent = Agent(
            make_config(), mode=AgentMode.AUTO_APPROVE, backend=HangingBackend()
        )

        async def consume() -> None:
            async for _ in agent.act("hello"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)

        assert agent.aborted is False
        agent.abort()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert agent.aborted is True
        assert agent._active_task is None

    asyncio.run(scenario())
