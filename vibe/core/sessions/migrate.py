from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from vibe.core.sessions.entries import (
    MessageEntry,
    SessionEntry,
    SessionHeader,
    UsageEntry,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore, SessionView
from vibe.core.types import LLMMessage


@dataclass(frozen=True)
class MigrateResult:
    source: Path
    target: Path | None = None
    backup: Path | None = None
    error: str | None = None


def _stable_message_id(index: int, raw: dict[str, Any]) -> str:
    payload = json.dumps(
        {"index": index, "message": raw},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{index:08d}{digest[:18]}"


def _fallback_session_id(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return f"legacy{digest[:18]}"


def read_legacy_json(path: Path) -> SessionView:
    data = json.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata") or {}
    raw_messages = data.get("messages") or []
    environment = metadata.get("environment") or {}
    start_time = str(metadata.get("start_time") or now_iso())

    header = SessionHeader(
        id=str(metadata.get("session_id") or _fallback_session_id(path)),
        timestamp=start_time,
        cwd=str(environment.get("working_directory") or ""),
        git_branch=metadata.get("git_branch"),
        git_commit=metadata.get("git_commit"),
        username=metadata.get("username"),
    )

    entries: list[SessionEntry] = []
    parent_id: str | None = header.id
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            continue
        entry_id = _stable_message_id(index, raw)
        entries.append(
            MessageEntry(
                id=entry_id,
                parent_id=parent_id,
                timestamp=start_time,
                message=LLMMessage.model_validate(raw),
            )
        )
        parent_id = entry_id

    stats = metadata.get("stats") or {}
    prompt_tokens = int(stats.get("session_prompt_tokens") or 0)
    completion_tokens = int(stats.get("session_completion_tokens") or 0)
    if prompt_tokens or completion_tokens:
        entries.append(
            UsageEntry(
                id=_stable_message_id(len(raw_messages), {"usage": True}),
                parent_id=parent_id,
                timestamp=start_time,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    return SessionView(header=header, entries=entries)


def migrate_file(path: Path, store: JsonlSessionStore) -> MigrateResult:
    view = read_legacy_json(path)
    workdir = Path(view.header.cwd) if view.header.cwd else path.parent
    target = store.session_path(workdir, view.header.id)
    if target.exists():
        return MigrateResult(
            source=path, error=f"Target session already exists: {target}"
        )
    store.write_entries(target, view.header, view.entries)
    backup = path.with_name(f"{path.name}.migrated")
    path.rename(backup)
    return MigrateResult(source=path, target=target, backup=backup)


def migrate_directory(directory: Path, store: JsonlSessionStore) -> list[MigrateResult]:
    if not directory.is_dir():
        return []
    results: list[MigrateResult] = []
    for path in sorted(directory.glob("*.json")):
        try:
            results.append(migrate_file(path, store))
        except Exception as exc:
            results.append(MigrateResult(source=path, error=str(exc)))
    return results
