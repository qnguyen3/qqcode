from __future__ import annotations

from pathlib import Path

from vibe.core.config import SessionLoggingConfig
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionHeader,
    UsageEntry,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, Role


def make_store(tmp_path: Path, *, fsync: bool = False) -> JsonlSessionStore:
    config = SessionLoggingConfig(
        enabled=True,
        fsync=fsync,
        sessions_dir=str(tmp_path),
        save_dir=str(tmp_path / "legacy"),
    )
    return JsonlSessionStore(config)


def make_header(session_id: str, cwd: Path) -> SessionHeader:
    return SessionHeader(id=session_id, timestamp=now_iso(), cwd=str(cwd))


def test_append_then_read_preserves_order_and_parents(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    header = make_header(session_id, workdir)
    path = store.session_path(workdir, session_id)
    store.write_entries(path, header, [])

    parent_id = header.id
    entries: list[MessageEntry] = []
    for index in range(5):
        entry = MessageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            message=LLMMessage(role=Role.user, content=f"message {index}"),
        )
        store.append(entry, path)
        entries.append(entry)
        parent_id = entry.id

    view = store.read(path)
    assert view.header.id == session_id
    assert view.entries == entries
    previous_id = header.id
    for current in entries:
        assert current.parent_id == previous_id
        previous_id = current.id


def test_append_without_path_uses_current_session(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    path = store.session_path(workdir, session_id)
    store.write_entries(path, make_header(session_id, workdir), [])
    assert store.current_path == path

    entry = MessageEntry(
        id=new_entry_id(),
        parent_id=session_id,
        timestamp=now_iso(),
        message=LLMMessage(role=Role.user, content="hello"),
    )
    store.append(entry)

    view = store.read(path)
    assert [item.id for item in view.entries] == [entry.id]


def test_file_growth_is_linear(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    path = store.session_path(workdir, session_id)
    store.write_entries(path, make_header(session_id, workdir), [])

    parent_id = session_id
    first_size = path.stat().st_size
    deltas: list[int] = []
    previous_size = first_size
    for index in range(100):
        entry = MessageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            message=LLMMessage(role=Role.user, content=f"entry {index:03d}"),
        )
        store.append(entry, path)
        size = path.stat().st_size
        if index > 0:
            deltas.append(size - previous_size)
        previous_size = size
        parent_id = entry.id

    assert len(path.read_text(encoding="utf-8").splitlines()) == 101
    assert max(deltas) - min(deltas) <= 8


def test_list_and_find_sessions(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    project_a = tmp_path / "alpha"
    project_a.mkdir()
    project_b = tmp_path / "beta"
    project_b.mkdir()

    session_a = "aaaaaaaa-0000-0000-0000-000000000000"
    path_a = store.session_path(project_a, session_a)
    store.write_entries(path_a, make_header(session_a, project_a), [])
    store.append(
        MessageEntry(
            id=new_entry_id(),
            parent_id=session_a,
            timestamp=now_iso(),
            message=LLMMessage(role=Role.user, content="first question"),
        ),
        path_a,
    )

    session_b = "bbbbbbbb-0000-0000-0000-000000000000"
    path_b = store.session_path(project_b, session_b)
    store.write_entries(path_b, make_header(session_b, project_b), [])

    assert store.find(session_a) == path_a
    assert store.find(session_a[:8]) == path_a
    assert store.find("does-not-exist") is None

    summaries_a = store.list(workdir=project_a, limit=10)
    assert [summary.path for summary in summaries_a] == [path_a]
    assert summaries_a[0].session_id == session_a
    assert summaries_a[0].message_count == 1
    assert summaries_a[0].last_user_message == "first question"

    all_summaries = store.list(workdir=None, limit=10)
    assert {summary.path for summary in all_summaries} == {path_a, path_b}

    limited = store.list(workdir=None, limit=1)
    assert len(limited) == 1


def test_usage_entries_are_summed_by_stats(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    path = store.session_path(workdir, session_id)
    store.write_entries(path, make_header(session_id, workdir), [])

    store.append(
        UsageEntry(
            id=new_entry_id(),
            parent_id=session_id,
            timestamp=now_iso(),
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.002,
        ),
        path,
    )
    store.append(
        UsageEntry(
            id=new_entry_id(),
            parent_id=session_id,
            timestamp=now_iso(),
            prompt_tokens=3,
            completion_tokens=4,
        ),
        path,
    )

    stats = store.read(path).stats()
    assert stats.session_prompt_tokens == 13
    assert stats.session_completion_tokens == 9
