from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent, context_overview, edit_context
from vibe.core.config import ModelConfig, ModelPricing, SessionLoggingConfig, VibeConfig
from vibe.core.sessions.entries import (
    ContextEditEntry,
    HeadMarkerEntry,
    MessageEntry,
    UsageEntry,
)
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import Role


def _make_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=True,
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
async def test_clear_history_records_head_marker_and_preserves_log_and_usage(
    tmp_path: Path,
) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="first reply",
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=10,
        ),
        mock_llm_chunk(
            content="second reply",
            finish_reason="stop",
            prompt_tokens=8,
            completion_tokens=4,
        ),
    ])
    agent = Agent(_make_config(tmp_path), backend=backend)
    original_session_id = agent.session_id

    async for _ in agent.act("hello before clear"):
        pass

    cost_before = agent.stats.session_cost
    assert len(agent.messages) == 3

    await agent.clear_history()

    assert agent.session_id == original_session_id
    assert [m.role for m in agent.messages] == [Role.system]
    assert agent.stats.session_prompt_tokens == 20
    assert agent.stats.session_completion_tokens == 10
    assert agent.stats.session_cost == cost_before

    store = JsonlSessionStore(agent.config.session_logging)
    session_path = store.find(agent.session_id)
    assert session_path is not None
    view = store.read(session_path)
    markers = [e for e in view.entries if isinstance(e, HeadMarkerEntry)]
    assert len(markers) == 1
    assert markers[0].reason == "clear"
    assert markers[0].keep_from_id is None
    assert len([e for e in view.entries if isinstance(e, MessageEntry)]) == 2
    assert len([e for e in view.entries if isinstance(e, UsageEntry)]) == 1
    assert view.messages() == []
    assert len(view.messages(include_compacted=True)) == 2

    async for _ in agent.act("hello after clear"):
        pass

    assert [m.content for m in agent.messages[1:]] == [
        "hello after clear",
        "second reply",
    ]


@pytest.mark.asyncio
async def test_agent_edit_context_and_overview(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="reply one", prompt_tokens=10, completion_tokens=5)
    ])
    agent = Agent(_make_config(tmp_path), backend=backend)

    async for _ in agent.act("first question"):
        pass

    user_entry_id = agent._entry_id_at(1)
    assistant_entry_id = agent._entry_id_at(2)
    assert user_entry_id is not None
    assert assistant_entry_id is not None

    edit_context(agent, user_entry_id, action="replace", replacement="edited question")
    edit_context(agent, assistant_entry_id, action="omit")

    assert [m.content for m in agent.messages[1:]] == ["edited question"]

    overview = context_overview(agent)
    assert overview.messages == 1
    assert overview.omitted == 1
    assert overview.replaced == 1
    assert "1 omitted, 1 replaced" in overview.describe()

    store = JsonlSessionStore(agent.config.session_logging)
    session_path = store.find(agent.session_id)
    assert session_path is not None
    view = store.read(session_path)
    edits = [e for e in view.entries if isinstance(e, ContextEditEntry)]
    assert len(edits) == 2
