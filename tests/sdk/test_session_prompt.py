from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import ModelConfig, ModelPricing, SessionLoggingConfig, VibeConfig
from vibe.sdk import (
    AgentOptions,
    MessageDeltaEvent,
    SessionStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
    create_agent,
)


def _make_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=False,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        ),
        system_prompt_id="tests",
        active_model="devstral-latest",
        models=[
            ModelConfig(
                name="mistral-vibe-cli-latest",
                provider="mistral",
                alias="devstral-latest",
                pricing=ModelPricing(input_per_million=0.4, output_per_million=2.0),
            )
        ],
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
    )


@pytest.mark.asyncio
async def test_session_prompt_emits_ordered_events_and_terminates(
    tmp_path: Path,
) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="Hello from SDK",
            finish_reason="stop",
            prompt_tokens=30,
            completion_tokens=12,
        )
    ])
    options = AgentOptions(
        config=_make_config(tmp_path),
        auto_approve=True,
        save_session=False,
        working_dir=tmp_path,
    )

    events = []
    async with create_agent(options, backend=backend) as session:
        async for ev in session.prompt("say hi"):
            events.append(ev)

        assert session.stats().session_prompt_tokens == 30
        assert session.stats().session_completion_tokens == 12
        assert [m.content for m in session.messages()[1:]] == [
            "say hi",
            "Hello from SDK",
        ]

    assert [ev.type for ev in events] == [
        "session.started",
        "turn.started",
        "message.delta",
        "turn.completed",
    ]
    assert isinstance(events[0], SessionStartedEvent)
    assert events[0].session_id == session.session_id
    assert isinstance(events[1], TurnStartedEvent)
    assert events[1].turn == 1
    assert isinstance(events[2], MessageDeltaEvent)
    assert events[2].delta == "Hello from SDK"
    assert events[2].content == "Hello from SDK"
    assert isinstance(events[3], TurnCompletedEvent)
    assert events[3].usage.input == 30
    assert events[3].usage.output == 12


@pytest.mark.asyncio
async def test_session_prompt_with_text_attachment(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("attachment body", encoding="utf-8")

    backend = FakeBackend([
        mock_llm_chunk(
            content="acknowledged",
            finish_reason="stop",
            prompt_tokens=15,
            completion_tokens=5,
        )
    ])
    options = AgentOptions(
        config=_make_config(tmp_path),
        auto_approve=True,
        save_session=False,
        working_dir=tmp_path,
    )

    async with create_agent(options, backend=backend) as session:
        events = [
            ev async for ev in session.prompt("check attachment", attachments=[note])
        ]

    assert any(isinstance(ev, MessageDeltaEvent) for ev in events)
    contents = [m.content for m in session.messages()[1:]]
    assert any("attachment body" in c for c in contents if c)
