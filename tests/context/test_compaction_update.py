from __future__ import annotations

from typing import Literal

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
from vibe.core.prompts import UtilityPrompt
from vibe.core.types import LLMMessage, Role


def make_agent(
    backend: FakeBackend, *, prompt: Literal["summary", "legacy"] = "summary"
) -> Agent:
    model = ModelConfig(
        name="test-model", provider="mistral", alias="test-compact", context_window=1
    )
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        active_model="test-compact",
        models=[model],
        compaction=CompactionConfig(
            reserve_tokens=0, keep_recent_tokens=150, prompt=prompt
        ),
    )
    return Agent(config, backend=backend)


def add_history(agent: Agent) -> None:
    for message in [
        LLMMessage(role=Role.user, content="old question " * 80),
        LLMMessage(role=Role.assistant, content="old answer " * 80),
        LLMMessage(role=Role.user, content="recent question " * 20),
        LLMMessage(role=Role.assistant, content="recent answer " * 20),
    ]:
        agent.add_message(message)


@pytest.mark.asyncio
async def test_second_compact_uses_update_prompt_and_keeps_previous_data() -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="FIRST SUMMARY"),
        mock_llm_chunk(content="SECOND SUMMARY"),
    ])
    agent = make_agent(backend)
    add_history(agent)

    await agent.compact()
    result = await agent.compactor.compact(force=True)

    assert result.is_update is True
    assert result.summary == "SECOND SUMMARY"

    request = backend.requests_messages[-1]
    combined = "\n".join(message.content or "" for message in request)
    assert "<previous_summary>" in combined
    assert "FIRST SUMMARY" in combined
    assert "Update the existing summary" in combined

    summary_message = agent.messages[1]
    assert (summary_message.content or "").startswith("<conversation_summary>")
    assert "SECOND SUMMARY" in (summary_message.content or "")


@pytest.mark.asyncio
async def test_legacy_prompt_disables_update_mode() -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="FIRST SUMMARY"),
        mock_llm_chunk(content="SECOND SUMMARY"),
    ])
    agent = make_agent(backend, prompt="legacy")
    add_history(agent)

    await agent.compact()
    result = await agent.compactor.compact(force=True)

    assert result.is_update is False
    request = backend.requests_messages[-1]
    combined = "\n".join(message.content or "" for message in request)
    assert "<previous_summary>" not in combined
    assert "comprehensive summary" in combined


def test_summary_prompts_render_required_sections() -> None:
    sections = (
        "## Goal",
        "## Constraints & Preferences",
        "## Progress",
        "## Key Decisions",
        "## Next Steps",
        "## Critical Context",
    )

    summary_prompt = UtilityPrompt.SUMMARY.read()
    update_prompt = UtilityPrompt.SUMMARY_UPDATE.read()

    for section in sections:
        assert section in summary_prompt
        assert section in update_prompt
    assert "<previous_summary>" in update_prompt
    assert "Preserve all existing information" in update_prompt
