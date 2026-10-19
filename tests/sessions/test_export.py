from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe.core.config import SessionLoggingConfig
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionEntry,
    SessionHeader,
    UsageEntry,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.export import (
    export_html,
    export_markdown,
    export_session,
    import_session,
)
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, Role


def make_config(tmp_path: Path) -> SessionLoggingConfig:
    return SessionLoggingConfig(
        enabled=True,
        fsync=False,
        sessions_dir=str(tmp_path / "sessions"),
        save_dir=str(tmp_path / "legacy"),
    )


def build_session(
    store: JsonlSessionStore, workdir: Path
) -> tuple[Path, list[LLMMessage]]:
    session_id = new_entry_id()
    header = SessionHeader(id=session_id, timestamp=now_iso(), cwd=str(workdir))
    path = store.session_path(workdir, session_id)
    store.write_entries(path, header, [])

    messages = [
        LLMMessage(role=Role.user, content="Hello **markdown**"),
        LLMMessage(role=Role.assistant, content="Hi <script>alert(1)</script>"),
        LLMMessage(role=Role.user, content="Bye"),
    ]
    entries: list[SessionEntry] = []
    parent_id = header.id
    for message in messages:
        entry = MessageEntry(
            id=new_entry_id(), parent_id=parent_id, timestamp=now_iso(), message=message
        )
        entries.append(entry)
        parent_id = entry.id
    entries.append(
        UsageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            prompt_tokens=11,
            completion_tokens=7,
        )
    )
    for entry in entries:
        store.append(entry, path)
    return path, messages


def test_export_markdown_matches_history(tmp_path: Path) -> None:
    store = JsonlSessionStore(make_config(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    _path, messages = build_session(store, workdir)
    metadata = {"session_id": "abc123", "model": "test-model"}

    document = export_markdown(messages, metadata)

    assert document.startswith("# Session abc123")
    assert "**Model**: test-model" in document
    assert "Hello **markdown**" in document
    assert "Hi <script>alert(1)</script>" in document
    assert document.count("### User") == 2
    assert document.count("### Assistant") == 1


def test_export_html_escapes_content(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    messages = [
        LLMMessage(role=Role.user, content="<b>bold</b>"),
        LLMMessage(role=Role.assistant, content="a & b"),
    ]

    document = export_html(messages, {"session_id": "s1"})

    assert document.startswith("<!DOCTYPE html>")
    assert "&lt;b&gt;bold&lt;/b&gt;" in document
    assert "a &amp; b" in document


def test_export_jsonl_copies_raw_entries(tmp_path: Path) -> None:
    store = JsonlSessionStore(make_config(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    source, messages = build_session(store, workdir)
    target = tmp_path / "export" / "session.jsonl"

    result = export_session(
        target, source=source, messages=messages, metadata={"session_id": "x"}
    )

    assert result == target
    exported = store.read(target)
    original = store.read(source)
    assert [entry.id for entry in exported.entries] == [
        entry.id for entry in original.entries
    ]


def test_export_jsonl_synthesizes_entries_without_source(tmp_path: Path) -> None:
    store = JsonlSessionStore(make_config(tmp_path))
    target = tmp_path / "synthetic.jsonl"
    messages = [
        LLMMessage(role=Role.user, content="one"),
        LLMMessage(role=Role.assistant, content="two"),
    ]

    export_session(
        target,
        source=None,
        messages=messages,
        metadata={"session_id": "synthetic-1", "cwd": str(tmp_path)},
    )

    view = store.read(target)
    assert view.header.id == "synthetic-1"
    assert [message.content for message in view.messages()] == ["one", "two"]


def test_export_session_markdown_default_suffix(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    messages = [LLMMessage(role=Role.user, content="hi")]

    export_session(target, source=None, messages=messages, metadata={})

    assert "hi" in target.read_text(encoding="utf-8")


def test_import_jsonl_roundtrip(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    source, messages = build_session(store, workdir)
    exported = tmp_path / "roundtrip.jsonl"
    export_session(
        exported, source=source, messages=messages, metadata={"session_id": "x"}
    )

    imported = import_session(exported, store=store, workdir=workdir)

    assert imported.path is not None
    assert imported.path != exported
    assert store.root in imported.path.parents
    assert [message.content for message in imported.messages] == [
        message.content for message in messages
    ]
    assert imported.stats is not None
    assert imported.stats.session_prompt_tokens == 11

    resumed = store.read(imported.path)
    assert [message.content for message in resumed.messages()] == [
        "Hello **markdown**",
        "Hi <script>alert(1)</script>",
        "Bye",
    ]


def test_import_legacy_json(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = JsonlSessionStore(config)
    workdir = tmp_path / "work"
    workdir.mkdir()
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps({
            "metadata": {"session_id": "legacy-1", "start_time": now_iso()},
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
        }),
        encoding="utf-8",
    )

    imported = import_session(legacy, store=store, workdir=workdir)

    assert imported.session_id == "legacy-1"
    assert imported.path is not None
    assert [message.content for message in imported.messages] == [
        "old question",
        "old answer",
    ]
    assert [message.content for message in store.read(imported.path).messages()] == [
        "old question",
        "old answer",
    ]


def test_import_without_persist_returns_no_path(tmp_path: Path) -> None:
    store = JsonlSessionStore(make_config(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()
    source, messages = build_session(store, workdir)

    imported = import_session(source, store=store, workdir=workdir, persist=False)

    assert imported.path is None
    assert len(imported.messages) == len(messages)


def test_import_rejects_missing_or_unknown_files(tmp_path: Path) -> None:
    store = JsonlSessionStore(make_config(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(ValueError):
        import_session(tmp_path / "nope.jsonl", store=store, workdir=workdir)

    weird = tmp_path / "session.txt"
    weird.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        import_session(weird, store=store, workdir=workdir)
