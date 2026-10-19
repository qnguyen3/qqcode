from __future__ import annotations

import json
from pathlib import Path

from vibe.core.config import SessionLoggingConfig
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.sessions.entries import MessageEntry, SessionHeader, UsageEntry, now_iso
from vibe.core.sessions.migrate import migrate_directory, migrate_file, read_legacy_json
from vibe.core.sessions.store import JsonlSessionStore

LEGACY_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "hello there"},
    {"role": "assistant", "content": "hi!"},
    {"role": "user", "content": "fix the bug"},
]


def make_config(tmp_path: Path) -> SessionLoggingConfig:
    return SessionLoggingConfig(
        enabled=True,
        fsync=False,
        save_dir=str(tmp_path / "legacy"),
        sessions_dir=str(tmp_path / "sessions"),
    )


def write_legacy(
    tmp_path: Path,
    config: SessionLoggingConfig,
    session_id: str,
    *,
    statistics: dict[str, int] | None = None,
) -> Path:
    save_dir = Path(config.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"session_20240101_000000_{session_id[:8]}.json"
    payload = {
        "metadata": {
            "session_id": session_id,
            "start_time": "2024-01-01T00:00:00",
            "end_time": "2024-01-01T00:05:00",
            "git_commit": "abc123",
            "git_branch": "main",
            "username": "tester",
            "environment": {"working_directory": str(tmp_path / "work")},
            "stats": statistics or {},
        },
        "messages": LEGACY_MESSAGES,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_session_reads_legacy_json(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = write_legacy(tmp_path, config, "11111111-2222-3333-4444-555555555555")

    messages, metadata = InteractionLogger.load_session(path)

    assert [message.content for message in messages] == [
        message["content"] for message in LEGACY_MESSAGES
    ]
    assert metadata["session_id"] == "11111111-2222-3333-4444-555555555555"


def test_read_legacy_json_generates_stable_ids(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = write_legacy(tmp_path, config, "abcdefgh-0000-0000-0000-000000000000")

    first = read_legacy_json(path)
    second = read_legacy_json(path)

    assert [entry.id for entry in first.entries] == [
        entry.id for entry in second.entries
    ]
    message_entries = [
        entry for entry in first.entries if isinstance(entry, MessageEntry)
    ]
    assert len(message_entries) == len(first.entries)
    assert [entry.message.content for entry in message_entries] == [
        message["content"] for message in LEGACY_MESSAGES
    ]
    assert first.header.git_branch == "main"
    assert first.header.cwd == str(tmp_path / "work")


def test_read_legacy_json_maps_stats_to_usage(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = write_legacy(
        tmp_path,
        config,
        "stats-session-0000",
        statistics={"session_prompt_tokens": 120, "session_completion_tokens": 30},
    )

    view = read_legacy_json(path)

    usage = [entry for entry in view.entries if isinstance(entry, UsageEntry)]
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 120
    assert usage[0].completion_tokens == 30
    assert view.stats().session_prompt_tokens == 120


def test_migrate_file_keeps_original_and_messages(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    path = write_legacy(
        tmp_path,
        config,
        "migrate-me-0000-0000",
        statistics={"session_prompt_tokens": 10, "session_completion_tokens": 2},
    )
    store = JsonlSessionStore(config)

    result = migrate_file(path, store)

    assert result.error is None
    assert result.target is not None and result.target.suffix == ".jsonl"
    assert result.backup is not None
    assert result.backup.name == f"{path.name}.migrated"
    assert result.backup.exists()
    assert not path.exists()

    view = store.read(result.target)
    assert [message.content for message in view.messages()] == [
        message["content"] for message in LEGACY_MESSAGES
    ]
    assert view.stats().session_prompt_tokens == 10


def test_migrate_directory_converts_all_json_files(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    for session_id in ("feed00000000", "beef00000000"):
        write_legacy(tmp_path, config, session_id)
    store = JsonlSessionStore(config)

    results = migrate_directory(Path(config.save_dir), store)

    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert all(result.target and result.target.exists() for result in results)
    assert list(Path(config.save_dir).glob("*.json")) == []


def test_list_and_find_sessions_across_formats(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    legacy_path = write_legacy(tmp_path, config, "legacy-id-000000")

    store = JsonlSessionStore(config)
    jsonl_header_path = store.session_path(tmp_path / "work", "jsonl-id-000000")
    store.write_entries(
        jsonl_header_path,
        SessionHeader(
            id="jsonl-id-000000", timestamp=now_iso(), cwd=str(tmp_path / "work")
        ),
        [],
    )

    found_legacy = InteractionLogger.find_session_by_id("legacy-id-000000", config)
    found_jsonl = InteractionLogger.find_session_by_id("jsonl-id-000000", config)
    assert found_legacy == legacy_path
    assert found_jsonl == jsonl_header_path

    sessions = InteractionLogger.list_sessions(config)
    paths = {path for path, _ in sessions}
    assert paths == {legacy_path, jsonl_header_path}
