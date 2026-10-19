from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import ModelConfig, ModelPricing, SessionLoggingConfig, VibeConfig
from vibe.core.sessions.entries import MessageEntry, ModelChangeEntry, UsageEntry
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import Role

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


def make_config(tmp_path: Path, *, active_model: str = "devstral-latest") -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        ),
        system_prompt_id="tests",
        active_model=active_model,
        models=MODELS,
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
    )


def read_view(agent: Agent):
    store = JsonlSessionStore(agent.config.session_logging)
    path = store.find(agent.session_id)
    assert path is not None
    return store.read(path)


@pytest.mark.asyncio
async def test_agent_records_messages_and_usage(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="hi there", prompt_tokens=12, completion_tokens=7)
    ])
    agent = Agent(make_config(tmp_path), backend=backend)

    async for _ in agent.act("hello"):
        pass

    view = read_view(agent)
    message_entries = [
        entry for entry in view.entries if isinstance(entry, MessageEntry)
    ]
    assert [entry.message.role for entry in message_entries] == [
        Role.user,
        Role.assistant,
    ]
    assert [entry.message.content for entry in message_entries] == ["hello", "hi there"]
    assert message_entries[0].parent_id == view.header.id
    assert message_entries[1].parent_id == message_entries[0].id

    usage_entries = [entry for entry in view.entries if isinstance(entry, UsageEntry)]
    assert len(usage_entries) == 1
    assert usage_entries[0].prompt_tokens == 12
    assert usage_entries[0].completion_tokens == 7
    assert usage_entries[0].cost_usd > 0
    assert usage_entries[0].parent_id == message_entries[1].id
    assert view.stats().session_prompt_tokens == 12
    assert view.stats().session_completion_tokens == 7


@pytest.mark.asyncio
async def test_agent_records_model_change(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="ok")])
    agent = Agent(make_config(tmp_path), backend=backend)

    async for _ in agent.act("hello"):
        pass

    assert read_view(agent).last_model() is None

    await agent.reload_with_initial_messages(
        config=make_config(tmp_path, active_model="devstral-small")
    )

    model_entry = read_view(agent).last_model()
    assert isinstance(model_entry, ModelChangeEntry)
    assert model_entry.model_alias == "devstral-small"
    assert model_entry.provider == "mistral"


@pytest.mark.asyncio
async def test_flush_does_not_duplicate_recorded_messages(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="ok")])
    agent = Agent(make_config(tmp_path), backend=backend)

    async for _ in agent.act("hello"):
        pass

    before = read_view(agent)
    await agent._recorder.flush()
    await agent._recorder.flush()
    after = read_view(agent)

    assert [entry.id for entry in after.entries] == [
        entry.id for entry in before.entries
    ]


@pytest.mark.asyncio
async def test_context_messages_share_ids_with_the_stored_log(tmp_path: Path) -> None:
    backend = FakeBackend([mock_llm_chunk(content="ok")])
    agent = Agent(make_config(tmp_path), backend=backend)

    async for _ in agent.act("hello"):
        pass

    stored_ids = [
        entry.id
        for entry in read_view(agent).entries
        if isinstance(entry, MessageEntry)
    ]
    assert [agent._entry_id_at(index) for index in (1, 2)] == stored_ids
