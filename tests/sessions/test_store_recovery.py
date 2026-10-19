from __future__ import annotations

from pathlib import Path

from vibe.core.config import SessionLoggingConfig
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionHeader,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, Role


def make_store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(
        SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        )
    )


def write_session(tmp_path: Path) -> tuple[JsonlSessionStore, Path, list[MessageEntry]]:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    path = store.session_path(workdir, session_id)
    store.write_entries(
        path, SessionHeader(id=session_id, timestamp=now_iso(), cwd=str(workdir)), []
    )
    entries: list[MessageEntry] = []
    parent_id = session_id
    for index in range(3):
        entry = MessageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            message=LLMMessage(role=Role.user, content=f"message {index}"),
        )
        store.append(entry, path)
        entries.append(entry)
        parent_id = entry.id
    return store, path, entries


def test_read_tolerates_truncated_last_line(tmp_path: Path) -> None:
    store, path, entries = write_session(tmp_path)
    with path.open("ab") as handle:
        handle.write(b'{"type": "message", "id": "abc"')

    view = store.read(path)

    assert view.truncated_at is not None
    assert view.entries == entries


def test_read_tolerates_corrupt_last_line(tmp_path: Path) -> None:
    store, path, entries = write_session(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    view = store.read(path)

    assert view.truncated_at is not None
    assert view.entries == entries


def test_read_stops_at_corrupt_middle_line(tmp_path: Path) -> None:
    store, path, entries = write_session(tmp_path)
    valid = path.read_text(encoding="utf-8")
    offset = len(valid)
    extra = MessageEntry(
        id=new_entry_id(),
        parent_id=entries[-1].id,
        timestamp=now_iso(),
        message=LLMMessage(role=Role.assistant, content="never replayed"),
    )
    path.write_text(
        f'{valid}{{"broken": true}}\n{extra.model_dump_json()}\n', encoding="utf-8"
    )

    view = store.read(path)

    assert view.truncated_at == offset
    assert view.entries == entries


def test_read_never_raises_on_missing_or_empty_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    missing = store.read(tmp_path / "missing.jsonl")
    assert missing.entries == []
    assert missing.truncated_at is None

    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert store.read(empty).entries == []

    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00\x01garbage")
    view = store.read(binary)
    assert view.entries == []
    assert view.truncated_at is not None


def test_repair_truncates_and_creates_backup(tmp_path: Path) -> None:
    store, path, entries = write_session(tmp_path)
    with path.open("ab") as handle:
        handle.write(b'{"type": "message", "id": "abc"')

    result = store.repair(path)

    assert result.removed_bytes > 0
    assert result.kept_entries == len(entries) + 1
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert result.backup_path.name == f"{path.name}.bak"

    repaired = store.read(path)
    assert repaired.entries == entries
    assert repaired.truncated_at is None


def test_repair_on_healthy_file_is_noop(tmp_path: Path) -> None:
    store, path, entries = write_session(tmp_path)

    result = store.repair(path)

    assert result.removed_bytes == 0
    assert result.backup_path is None
    assert store.read(path).entries == entries
