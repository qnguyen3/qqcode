from __future__ import annotations

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    CompactionConfig,
    ModelConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.context.tokens import estimate_context_tokens
from vibe.core.types import LLMMessage, Role

OLD_USER = "old question " * 80
OLD_ASSISTANT = "old answer " * 80
RECENT_USER = "recent question " * 20
RECENT_ASSISTANT = "recent answer " * 20


def make_agent(backend: FakeBackend, *, keep_recent_tokens: int = 150) -> Agent:
    model = ModelConfig(
        name="test-model", provider="mistral", alias="test-compact", context_window=1
    )
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        active_model="test-compact",
        models=[model],
        compaction=CompactionConfig(
            reserve_tokens=0, keep_recent_tokens=keep_recent_tokens
        ),
    )
    return Agent(config, backend=backend)


def add_history(agent: Agent) -> None:
    for message in [
        LLMMessage(role=Role.user, content=OLD_USER),
        LLMMessage(role=Role.assistant, content=OLD_ASSISTANT),
        LLMMessage(role=Role.user, content=RECENT_USER),
        LLMMessage(role=Role.assistant, content=RECENT_ASSISTANT),
    ]:
        agent.add_message(message)


def context_estimate(agent: Agent) -> int:
    tools = agent.format_handler.get_available_tools(agent.tool_manager, agent.config)
    return estimate_context_tokens(agent.messages, tools)


@pytest.mark.asyncio
async def test_compact_replaces_only_the_old_segment() -> None:
    backend = FakeBackend([mock_llm_chunk(content="SUMMARY")])
    agent = make_agent(backend)
    add_history(agent)
    before = context_estimate(agent)

    summary = await agent.compact()

    assert summary == "SUMMARY"
    assert agent.messages[0].role is Role.system
    summary_message = agent.messages[1]
    assert summary_message.role is Role.user
    assert (summary_message.content or "").startswith("<conversation_summary>")
    assert "SUMMARY" in (summary_message.content or "")

    tail_contents = [message.content for message in agent.messages[2:]]
    assert tail_contents == [RECENT_USER, RECENT_ASSISTANT]
    assert context_estimate(agent) < before


@pytest.mark.asyncio
async def test_compact_does_not_call_count_tokens() -> None:
    backend = FakeBackend([mock_llm_chunk(content="SUMMARY")])
    agent = make_agent(backend)
    add_history(agent)

    await agent.compact()

    assert backend.count_tokens_calls == []


@pytest.mark.asyncio
async def test_compactor_result_reports_cut_and_token_delta() -> None:
    backend = FakeBackend([mock_llm_chunk(content="SUMMARY")])
    agent = make_agent(backend)
    add_history(agent)

    result = await agent.compactor.compact(force=True)

    assert result.summary == "SUMMARY"
    assert result.cut.first_kept_index == 2
    assert result.cut.is_split_turn is False
    assert result.tokens_before > result.tokens_after_estimate
    assert result.is_update is False
    assert result.usage is not None


@pytest.mark.asyncio
async def test_compact_forwards_user_instructions() -> None:
    backend = FakeBackend([mock_llm_chunk(content="SUMMARY")])
    agent = make_agent(backend)
    add_history(agent)

    await agent.compact(instructions="focus on the failing tests")

    request = backend.requests_messages[-1]
    assert any(
        "focus on the failing tests" in (message.content or "") for message in request
    )
