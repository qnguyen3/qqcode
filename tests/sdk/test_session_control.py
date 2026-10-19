from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.mock.mock_backend_factory import mock_backend_factory
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import (
    Backend,
    CompactionConfig,
    ModelConfig,
    ModelPricing,
    SessionLoggingConfig,
    ThinkingLevel,
    VibeConfig,
)
from vibe.sdk import AgentOptions, SessionCompactedEvent, create_agent

MODELS = [
    ModelConfig(
        name="mistral-vibe-cli-latest",
        provider="mistral",
        alias="devstral-latest",
        pricing=ModelPricing(input_per_million=0.4, output_per_million=2.0),
    ),
    ModelConfig(
        name="devstral-small-latest",
        provider="mistral",
        alias="devstral-small",
        pricing=ModelPricing(input_per_million=0.1, output_per_million=0.3),
    ),
]


def _make_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=False,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        ),
        compaction=CompactionConfig(keep_recent_tokens=1),
        system_prompt_id="tests",
        active_model="devstral-latest",
        models=MODELS,
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
    )


@pytest.mark.asyncio
async def test_session_steer_injects_message(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="turn one response",
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        ),
        mock_llm_chunk(
            content="steered response",
            finish_reason="stop",
            prompt_tokens=15,
            completion_tokens=6,
        ),
    ])
    options = AgentOptions(
        config=_make_config(tmp_path),
        auto_approve=True,
        save_session=False,
        working_dir=tmp_path,
    )

    async with create_agent(options, backend=backend) as session:
        await session.steer("also mention Python 3.12")
        async for _ in session.prompt("start"):
            pass

        contents = [m.content for m in session.messages()[1:]]
        assert "also mention Python 3.12" in contents
        assert "steered response" in contents


@pytest.mark.asyncio
async def test_session_set_model_and_thinking_and_compact(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="long explanation " * 40,
            finish_reason="stop",
            prompt_tokens=10000,
            completion_tokens=500,
        ),
        mock_llm_chunk(
            content="short summary",
            finish_reason="stop",
            prompt_tokens=50,
            completion_tokens=10,
        ),
    ])
    with mock_backend_factory(Backend.MISTRAL, lambda provider, **kwargs: backend):
        options = AgentOptions(
            config=_make_config(tmp_path),
            auto_approve=True,
            save_session=False,
            working_dir=tmp_path,
        )
        async with create_agent(options, backend=backend) as session:
            await session.set_model("devstral-small")
            assert session.agent.config.active_model == "devstral-small"

            await session.set_thinking("high")
            assert session.agent.thinking_level is ThinkingLevel.HIGH

            async for _ in session.prompt("explain the architecture in detail"):
                pass

            compacted_events: list[SessionCompactedEvent] = []
            session.bus.subscribe(
                lambda ev: compacted_events.append(ev)
                if isinstance(ev, SessionCompactedEvent)
                else None
            )
            summary = await session.compact(instructions="keep it brief")
            assert "short summary" in summary
            assert len(compacted_events) == 1
            assert compacted_events[0].tokens_after <= compacted_events[0].tokens_before


@pytest.mark.asyncio
async def test_session_abort_cancels_in_flight_turn(tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowBackend(FakeBackend):
        async def complete(self, **kwargs):
            started.set()
            await asyncio.sleep(5.0)
            return await super().complete(**kwargs)

    options = AgentOptions(
        config=_make_config(tmp_path),
        auto_approve=True,
        save_session=False,
        working_dir=tmp_path,
    )
    async with create_agent(options, backend=SlowBackend()) as session:

        async def _consume() -> None:
            async for _ in session.prompt("slow request"):
                pass

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await session.abort()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
