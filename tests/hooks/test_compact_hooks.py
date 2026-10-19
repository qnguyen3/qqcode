from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.hooks.api import deny, modify
from vibe.core.hooks.events import (
    AfterCompactEvent,
    BeforeCompactEvent,
    CompactFailedEvent,
    HookDecision,
)
from vibe.core.types import LLMChunk, LLMMessage


def make_agent(tmp_path: Path, backend: FakeBackend) -> Agent:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        enabled_tools=[],
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        workdir=tmp_path,
    )
    return Agent(config, auto_approve=True, backend=backend)


async def start_conversation(agent: Agent) -> None:
    async for _ in agent.act("hi"):
        pass


def conversation_snapshot(agent: Agent) -> list[LLMMessage]:
    return [message.model_copy(deep=True) for message in agent.messages]


def turn_chunk() -> LLMChunk:
    return mock_llm_chunk(content="Hello.", finish_reason="stop")


def summary_chunk() -> LLMChunk:
    return mock_llm_chunk(content="Summarized.", finish_reason="stop")


@pytest.mark.asyncio
async def test_before_compact_deny_skips_compaction(tmp_path: Path) -> None:
    backend = FakeBackend([turn_chunk()])
    agent = make_agent(tmp_path, backend)
    await start_conversation(agent)
    before = conversation_snapshot(agent)
    requests_before = len(backend.requests_messages)
    seen: list[BeforeCompactEvent] = []

    def veto(event: BeforeCompactEvent) -> HookDecision:
        seen.append(event)
        return deny("not now")

    agent.hooks.register("before_compact", veto)

    summary = await agent.compact()

    assert summary == ""
    assert seen[0].reason == "manual"
    assert len(backend.requests_messages) == requests_before
    assert conversation_snapshot(agent) == before


@pytest.mark.asyncio
async def test_summary_override_replaces_the_summarizer_call(tmp_path: Path) -> None:
    backend = FakeBackend([turn_chunk()])
    agent = make_agent(tmp_path, backend)
    await start_conversation(agent)
    requests_before = len(backend.requests_messages)
    agent.hooks.register(
        "before_compact", lambda event: modify(summary_override="HOOK SUMMARY")
    )

    summary = await agent.compact()

    assert summary == "HOOK SUMMARY"
    assert len(backend.requests_messages) == requests_before
    assert any("HOOK SUMMARY" in (message.content or "") for message in agent.messages)


@pytest.mark.asyncio
async def test_after_compact_observes_the_result(tmp_path: Path) -> None:
    backend = FakeBackend([turn_chunk(), summary_chunk()])
    agent = make_agent(tmp_path, backend)
    await start_conversation(agent)
    observed: list[AfterCompactEvent] = []
    agent.hooks.register("after_compact", lambda event: observed.append(event))

    summary = await agent.compact()

    assert summary.startswith("Summarized.")
    assert len(observed) == 1
    assert observed[0].is_update is False
    assert observed[0].tokens_after >= 0


@pytest.mark.asyncio
async def test_compact_failed_reports_the_error_and_propagates(tmp_path: Path) -> None:
    backend = FakeBackend(
        [turn_chunk()], exceptions=[None, RuntimeError("summarizer exploded")]
    )
    agent = make_agent(tmp_path, backend)
    await start_conversation(agent)
    failures: list[CompactFailedEvent] = []
    agent.hooks.register("compact_failed", lambda event: failures.append(event))

    with pytest.raises(RuntimeError):
        await agent.compact()

    assert len(failures) == 1
    assert "summarizer exploded" in failures[0].error


@pytest.mark.asyncio
async def test_broken_compact_hook_does_not_break_compaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    backend = FakeBackend([turn_chunk(), summary_chunk()])
    agent = make_agent(tmp_path, backend)
    await start_conversation(agent)

    def broken(event: BeforeCompactEvent) -> HookDecision:
        raise RuntimeError("hook exploded")

    agent.hooks.register("before_compact", broken)

    with caplog.at_level(logging.WARNING, logger="vibe"):
        summary = await agent.compact()

    assert summary.startswith("Summarized.")
    assert any("hook exploded" in record.message for record in caplog.records)
