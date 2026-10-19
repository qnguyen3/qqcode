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
from vibe.core.middleware import (
    ContextWarningMiddleware,
    ConversationContext,
    MiddlewareAction,
)
from vibe.core.types import AgentStats
from vibe.core.utils import VIBE_WARNING_TAG


def make_context_tokens(tokens: int, window: int) -> ConversationContext:
    return ConversationContext(
        messages=[],
        stats=AgentStats(context_tokens=tokens, context_window=window),
        config=VibeConfig(session_logging=SessionLoggingConfig(enabled=False)),
    )


@pytest.mark.asyncio
async def test_warns_at_70_and_90_once_per_level() -> None:
    middleware = ContextWarningMiddleware(max_context=1000)

    warning_70 = await middleware.before_turn(make_context_tokens(700, 1000))
    assert warning_70.action == MiddlewareAction.INJECT_MESSAGE
    assert warning_70.message is not None
    assert "70%" in warning_70.message
    assert "90%" not in warning_70.message

    repeated_70 = await middleware.before_turn(make_context_tokens(750, 1000))
    assert repeated_70.action == MiddlewareAction.CONTINUE

    warning_90 = await middleware.before_turn(make_context_tokens(900, 1000))
    assert warning_90.action == MiddlewareAction.INJECT_MESSAGE
    assert warning_90.message is not None
    assert "90%" in warning_90.message
    assert (await middleware.before_turn(make_context_tokens(950, 1000))).action == (
        MiddlewareAction.CONTINUE
    )

    middleware.reset()
    after_reset = await middleware.before_turn(make_context_tokens(950, 1000))
    assert after_reset.action == MiddlewareAction.INJECT_MESSAGE


@pytest.mark.asyncio
async def test_no_warning_below_threshold() -> None:
    middleware = ContextWarningMiddleware(max_context=1000)

    result = await middleware.before_turn(make_context_tokens(699, 1000))

    assert result.action == MiddlewareAction.CONTINUE


def make_config(*, context_warnings: bool) -> VibeConfig:
    model = ModelConfig(
        name="test-model", provider="mistral", alias="test-warn", context_window=10
    )
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        active_model="test-warn",
        models=[model],
        compaction=CompactionConfig(enabled=False),
        context_warnings=context_warnings,
    )


@pytest.mark.asyncio
async def test_agent_injects_each_warning_once_per_turn() -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="ok", finish_reason="stop"),
        mock_llm_chunk(content="ok again", finish_reason="stop"),
    ])
    agent = Agent(make_config(context_warnings=True), backend=backend)

    [_ async for _ in agent.act("Hello")]
    first_turn = agent.messages[1].content or ""
    assert first_turn.count(f"<{VIBE_WARNING_TAG}>") == 2

    [_ async for _ in agent.act("Again")]
    second_turn = agent.messages[3].content or ""
    assert second_turn.count(f"<{VIBE_WARNING_TAG}>") == 2


@pytest.mark.asyncio
async def test_agent_does_not_warn_when_disabled() -> None:
    backend = FakeBackend([mock_llm_chunk(content="ok", finish_reason="stop")])
    agent = Agent(make_config(context_warnings=False), backend=backend)

    [_ async for _ in agent.act("Hello")]

    assert VIBE_WARNING_TAG not in (agent.messages[1].content or "")
