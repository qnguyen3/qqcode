from __future__ import annotations

from pathlib import Path

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
from vibe.core.sessions.entries import (
    CompactionEntry,
    MessageEntry,
    SessionHeader,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore, SessionView
from vibe.core.types import LLMMessage, Role

OLD_USER = "old question " * 80
OLD_ASSISTANT = "old answer " * 80
RECENT_USER = "recent question " * 20
RECENT_ASSISTANT = "recent answer " * 20


def build_view() -> tuple[SessionView, list[MessageEntry]]:
    session_id = new_entry_id()
    header = SessionHeader(id=session_id, timestamp=now_iso(), cwd="/tmp")
    messages = [
        LLMMessage(role=Role.user, content="old user"),
        LLMMessage(role=Role.assistant, content="old assistant"),
        LLMMessage(role=Role.user, content="recent user"),
        LLMMessage(role=Role.assistant, content="recent assistant"),
    ]
    entries: list[MessageEntry] = []
    parent_id = header.id
    for message in messages:
        entry = MessageEntry(
            id=new_entry_id(), parent_id=parent_id, timestamp=now_iso(), message=message
        )
        entries.append(entry)
        parent_id = entry.id

    compaction = CompactionEntry(
        id=new_entry_id(),
        parent_id=parent_id,
        timestamp=now_iso(),
        first_kept_id=entries[2].id,
        summary="what happened before",
        tokens_before=1000,
        tokens_after=100,
    )
    view = SessionView(header=header, entries=[*entries, compaction])
    return view, entries


def test_replay_honors_first_kept_id() -> None:
    view, entries = build_view()

    messages = view.messages()

    assert len(messages) == 3
    assert messages[0].role is Role.user
    assert "<conversation_summary>" in (messages[0].content or "")
    assert "what happened before" in (messages[0].content or "")
    assert messages[1].content == "recent user"
    assert messages[2].content == "recent assistant"


def test_replay_without_first_kept_id_drops_all_messages() -> None:
    view, _ = build_view()
    compaction = view.entries[-1]
    assert isinstance(compaction, CompactionEntry)
    compaction.first_kept_id = None

    messages = view.messages()

    assert len(messages) == 1
    assert "<conversation_summary>" in (messages[0].content or "")


def test_include_compacted_returns_raw_history() -> None:
    view, entries = build_view()

    messages = view.messages(include_compacted=True)

    assert [message.content for message in messages] == [
        "old user",
        "old assistant",
        "recent user",
        "recent assistant",
    ]


def make_agent(tmp_path: Path) -> Agent:
    model = ModelConfig(
        name="test-model", provider="mistral", alias="test-compact", context_window=1
    )
    config = VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        ),
        active_model="test-compact",
        models=[model],
        compaction=CompactionConfig(reserve_tokens=0, keep_recent_tokens=150),
    )
    backend = FakeBackend([mock_llm_chunk(content="SUMMARY")])
    return Agent(config, backend=backend)


@pytest.mark.asyncio
async def test_replay_matches_runtime_history_after_compaction(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    for message in (
        LLMMessage(role=Role.user, content=OLD_USER),
        LLMMessage(role=Role.assistant, content=OLD_ASSISTANT),
        LLMMessage(role=Role.user, content=RECENT_USER),
        LLMMessage(role=Role.assistant, content=RECENT_ASSISTANT),
    ):
        agent.add_message(message)

    await agent.compact()

    store = JsonlSessionStore(agent.config.session_logging)
    path = store.find(agent.session_id)
    assert path is not None
    view = store.read(path)

    replayed = [message.model_dump() for message in view.messages()]
    runtime = [message.model_dump() for message in agent.messages[1:]]
    assert replayed == runtime
    assert agent.messages[0].content not in [
        message.content for message in view.messages()
    ]
