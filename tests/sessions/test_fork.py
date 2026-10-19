from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.config import SessionLoggingConfig
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionEntry,
    SessionHeader,
    UsageEntry,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore, SessionEntryNotFoundError
from vibe.core.types import LLMMessage, Role


def make_config(tmp_path: Path) -> SessionLoggingConfig:
    return SessionLoggingConfig(
        enabled=True,
        fsync=False,
        sessions_dir=str(tmp_path / "sessions"),
        save_dir=str(tmp_path / "legacy"),
    )


def build_source(
    store: JsonlSessionStore, workdir: Path
) -> tuple[Path, SessionHeader, list[SessionEntry]]:
    session_id = new_entry_id()
    header = SessionHeader(id=session_id, timestamp=now_iso(), cwd=str(workdir))
    path = store.session_path(workdir, session_id)
    store.write_entries(path, header, [])

    entries: list[SessionEntry] = []
    parent_id = header.id
    for index in range(4):
        entry = MessageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            message=LLMMessage(role=Role.user, content=f"message {index}"),
        )
        entries.append(entry)
        parent_id = entry.id
    usage = UsageEntry(
        id=new_entry_id(),
        parent_id=parent_id,
        timestamp=now_iso(),
        prompt_tokens=99,
        completion_tokens=11,
    )
    entries.append(usage)
    for entry in entries:
        store.append(entry, path)
    return path, header, entries


def test_fork_copies_entries_up_to_point_and_new_header(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    source_workdir = tmp_path / "source"
    source_workdir.mkdir()
    source_path, source_header, entries = build_source(store, source_workdir)
    message_entries = [entry for entry in entries if isinstance(entry, MessageEntry)]

    fork_workdir = tmp_path / "fork"
    fork_workdir.mkdir()
    forked_path = store.fork(source_path, message_entries[1].id, workdir=fork_workdir)

    assert forked_path != source_path
    view = store.read(forked_path)
    assert view.header.id != source_header.id
    assert view.header.parent_session_id == source_header.id
    assert view.header.cwd == str(fork_workdir)
    assert [entry.id for entry in view.entries] == [
        message_entries[0].id,
        message_entries[1].id,
    ]
    assert not any(isinstance(entry, UsageEntry) for entry in view.entries)


def test_fork_can_continue_appending(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    source_path, _, entries = build_source(store, workdir)
    message_entries = [entry for entry in entries if isinstance(entry, MessageEntry)]

    forked_path = store.fork(source_path, message_entries[0].id, workdir=workdir)

    forked_view = store.read(forked_path)
    follow_up = MessageEntry(
        id=new_entry_id(),
        parent_id=forked_view.entries[-1].id,
        timestamp=now_iso(),
        message=LLMMessage(role=Role.user, content="follow up"),
    )
    store.append(follow_up, forked_path)

    resumed = store.read(forked_path)
    assert [entry.id for entry in resumed.entries] == [
        message_entries[0].id,
        follow_up.id,
    ]
    assert resumed.header.parent_session_id is not None


def test_resume_fork_with_interaction_logger(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    source_path, _, entries = build_source(store, workdir)
    message_entries = [entry for entry in entries if isinstance(entry, MessageEntry)]
    forked_path = store.fork(source_path, message_entries[2].id, workdir=workdir)
    forked_view = store.read(forked_path)

    logger = InteractionLogger(
        config,
        session_id=forked_view.header.id,
        workdir=workdir,
        existing_filepath=forked_path,
    )
    logger.append_message(LLMMessage(role=Role.assistant, content="resumed!"))

    resumed = store.read(forked_path)
    assert [type(entry) for entry in resumed.entries[-2:]] == [
        MessageEntry,
        MessageEntry,
    ]
    last_entry = resumed.entries[-1]
    assert isinstance(last_entry, MessageEntry)
    assert last_entry.message.content == "resumed!"


def test_fork_at_unknown_entry_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    source_path, _, _ = build_source(store, workdir)

    with pytest.raises(SessionEntryNotFoundError):
        store.fork(source_path, "missing-entry", workdir=workdir)


def test_fork_point_none_returns_full_view(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    source_path, _, _ = build_source(store, workdir)

    view = store.read(source_path)
    assert view.fork_point(None) is view
