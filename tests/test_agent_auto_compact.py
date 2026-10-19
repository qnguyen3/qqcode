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
from vibe.core.types import (
    AssistantEvent,
    CompactEndEvent,
    CompactStartEvent,
    ContextUsageEvent,
    LLMMessage,
    Role,
)


@pytest.mark.asyncio
async def test_auto_compact_triggers_and_keeps_recent_window() -> None:
    observed: list[tuple[Role, str | None]] = []

    def observer(msg: LLMMessage) -> None:
        observed.append((msg.role, msg.content))

    backend = FakeBackend([
        mock_llm_chunk(content="<summary>"),
        mock_llm_chunk(content="<final>"),
    ])
    # Create a model with a low context_limit to trigger compaction
    test_model = ModelConfig(
        name="test-model", provider="mistral", alias="test-compact", context_window=1
    )
    cfg = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        active_model="test-compact",
        models=[test_model],
        compaction=CompactionConfig(reserve_tokens=0, keep_recent_tokens=150),
    )
    agent = Agent(cfg, message_observer=observer, backend=backend)

    old_user = "old question " * 80
    old_assistant = "old answer " * 80
    recent_user = "recent question " * 20
    recent_assistant = "recent answer " * 20
    for message in [
        LLMMessage(role=Role.user, content=old_user),
        LLMMessage(role=Role.assistant, content=old_assistant),
        LLMMessage(role=Role.user, content=recent_user),
        LLMMessage(role=Role.assistant, content=recent_assistant),
    ]:
        agent.add_message(message)

    events = [ev async for ev in agent.act("Hello")]

    assert [type(event) for event in events] == [
        CompactStartEvent,
        CompactEndEvent,
        AssistantEvent,
        ContextUsageEvent,
    ]
    assert isinstance(events[0], CompactStartEvent)
    assert isinstance(events[1], CompactEndEvent)
    assert isinstance(events[2], AssistantEvent)
    assert isinstance(events[3], ContextUsageEvent)
    start = events[0]
    end = events[1]
    final = events[2]
    usage_event = events[3]
    assert usage_event.context_window > 0
    assert usage_event.context_tokens > 0
    assert usage_event.context_percent >= 0
    assert start.threshold == 1
    assert end.old_context_tokens == start.current_context_tokens
    assert end.new_context_tokens < end.old_context_tokens
    assert final.content == "<final>"

    assert agent.messages[0].role == Role.system
    summary_message = agent.messages[1]
    assert summary_message.role == Role.user
    assert "<conversation_summary>" in (summary_message.content or "")
    assert "<summary>" in (summary_message.content or "")
    tail_contents = [msg.content for msg in agent.messages[2:]]
    assert recent_user in tail_contents
    assert recent_assistant in tail_contents
    assert old_user not in tail_contents
    assert old_assistant not in tail_contents

    roles = [role for role, _ in observed]
    assert roles[0] == Role.system
    assert roles[-1] == Role.assistant
    assert observed[-1][1] == "<final>"
