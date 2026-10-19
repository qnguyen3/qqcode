from __future__ import annotations

from pathlib import Path

from vibe.core.sessions.entries import MessageEntry, SessionHeader
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, Role


def _msg(entry_id: str, text: str) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        timestamp="2026-01-01T00:00:00Z",
        message=LLMMessage(role=Role.user, content=text),
    )


def test_read_since_returns_entries_after_anchor(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    path = store.session_path(tmp_path, "sess1")
    header = SessionHeader(
        id="sess1", timestamp="2026-01-01T00:00:00Z", cwd=str(tmp_path)
    )
    m1 = _msg("m1", "one")
    m2 = _msg("m2", "two")
    m3 = _msg("m3", "three")
    store.write_entries(path, header, [m1, m2])
    store.append(m3, path)

    tail = store.read_since(path, "m1")
    assert tail.anchor_found is True
    assert [e.id for e in tail.entries] == ["m2", "m3"]

    all_entries = store.read_since(path, None)
    assert all_entries.anchor_found is True
    assert [e.id for e in all_entries.entries] == ["m1", "m2", "m3"]


def test_read_since_handles_missing_anchor_and_truncated_tail(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    path = store.session_path(tmp_path, "sess2")
    header = SessionHeader(
        id="sess2", timestamp="2026-01-01T00:00:00Z", cwd=str(tmp_path)
    )
    m1 = _msg("m1", "one")
    m2 = _msg("m2", "two")
    store.write_entries(path, header, [m1, m2])

    missing = store.read_since(path, "unknown-id")
    assert missing.anchor_found is False
    assert missing.entries == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message","id":"broken"')

    tail = store.read_since(path, "m1")
    assert tail.anchor_found is True
    assert [e.id for e in tail.entries] == ["m2"]

    nonexistent = store.read_since(tmp_path / "does-not-exist.jsonl", "m1")
    assert nonexistent.anchor_found is False
    assert nonexistent.entries == []
