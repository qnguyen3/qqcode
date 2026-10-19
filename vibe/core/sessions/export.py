from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
import shutil
from typing import Any

from vibe.core.interaction_logger import InteractionLogger
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionEntry,
    SessionHeader,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JSONL_SUFFIX, JsonlSessionStore
from vibe.core.types import AgentStats, LLMMessage, Role, message_for_storage

__all__ = [
    "ImportedSession",
    "export_html",
    "export_markdown",
    "export_raw_jsonl",
    "export_session",
    "import_session",
]


@dataclass(frozen=True)
class ImportedSession:
    """Session loaded from a user supplied file, ready to be resumed."""

    session_id: str
    path: Path | None
    messages: list[LLMMessage]
    stats: AgentStats | None


def _metadata_lines(metadata: dict[str, Any]) -> list[str]:
    fields = [
        ("Session ID", metadata.get("session_id")),
        ("Model", metadata.get("model")),
        ("Started", metadata.get("start_time")),
        ("Ended", metadata.get("end_time")),
        ("Working directory", metadata.get("cwd")),
        ("Parent session", metadata.get("parent_session_id")),
    ]
    return [f"- **{label}**: {value}" for label, value in fields if value]


def _tool_call_names(message: LLMMessage) -> list[str]:
    names: list[str] = []
    for tool_call in message.tool_calls or []:
        if tool_call.function and tool_call.function.name:
            names.append(tool_call.function.name)
    return names


def _markdown_message(message: LLMMessage) -> list[str]:
    content = (message.content or "").strip()
    match message.role:
        case Role.system:
            return []
        case Role.user:
            return [f"### User\n\n{content}"] if content else []
        case Role.assistant:
            blocks: list[str] = []
            if content:
                blocks.append(f"### Assistant\n\n{content}")
            for name in _tool_call_names(message):
                blocks.append(f"### Tool call\n\n`{name}`")
            return blocks
        case Role.tool:
            label = message.name or "tool"
            return [f"### Tool result: {label}\n\n{content}"] if content else []


def export_markdown(
    messages: list[LLMMessage],
    metadata: dict[str, Any] | None = None,
    *,
    title: str | None = None,
) -> str:
    """Render conversation ``messages`` as a self-contained markdown document."""
    metadata = metadata or {}
    heading = title or f"Session {metadata.get('session_id', '')}".strip()
    lines: list[str] = [f"# {heading}", ""]

    if meta_lines := _metadata_lines(metadata):
        lines.extend(meta_lines)
        lines.append("")

    lines.append(f"_Exported {datetime.now(UTC).isoformat()}_")
    lines.append("")

    for message in messages:
        lines.extend(_markdown_message(message))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def export_html(
    messages: list[LLMMessage],
    metadata: dict[str, Any] | None = None,
    *,
    title: str | None = None,
) -> str:
    """Render conversation ``messages`` as a minimal, self-contained HTML page."""
    metadata = metadata or {}
    heading = title or f"Session {metadata.get('session_id', '')}".strip()

    meta_html = "".join(
        f"<li><strong>{escape(label)}</strong>: {escape(str(value))}</li>"
        for label, value in [
            ("Session ID", metadata.get("session_id")),
            ("Model", metadata.get("model")),
            ("Started", metadata.get("start_time")),
            ("Ended", metadata.get("end_time")),
        ]
        if value
    )

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{escape(heading)}</title>",
        "<style>",
        "body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; padding: 0 1rem; }",
        ".message { border-left: 3px solid #d0d0d0; margin: 1rem 0; padding: 0.5rem 1rem; }",
        ".user { border-color: #3b82f6; }",
        ".assistant { border-color: #10b981; }",
        ".tool { border-color: #f59e0b; }",
        "pre { white-space: pre-wrap; word-wrap: break-word; font-family: inherit; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{escape(heading)}</h1>",
    ]
    if meta_html:
        parts.append(f"<ul>{meta_html}</ul>")

    for message in messages:
        if message.role is Role.system:
            continue
        content = escape(message.content or "")
        if message.role is Role.tool:
            label = escape(message.name or "tool")
            parts.append(
                f'<div class="message tool"><h3>Tool result: {label}</h3>'
                f"<pre>{content}</pre></div>"
            )
            continue
        role = "user" if message.role is Role.user else "assistant"
        label = "User" if message.role is Role.user else "Assistant"
        parts.append(
            f'<div class="message {role}"><h3>{label}</h3><pre>{content}</pre></div>'
        )
        for name in _tool_call_names(message):
            parts.append(
                f'<div class="message tool"><h3>Tool call</h3>'
                f"<pre>{escape(name)}</pre></div>"
            )

    parts.extend(["</body>", "</html>", ""])
    return "\n".join(parts)


def export_raw_jsonl(source: Path, target: Path) -> Path:
    """Copy a ``.jsonl`` session file verbatim to ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _write_synthetic_jsonl(
    target: Path, messages: list[LLMMessage], metadata: dict[str, Any]
) -> None:
    header = SessionHeader(
        id=str(metadata.get("session_id") or new_entry_id()),
        timestamp=str(metadata.get("start_time") or now_iso()),
        cwd=str(metadata.get("cwd") or ""),
        git_branch=metadata.get("git_branch"),
        git_commit=metadata.get("git_commit"),
        parent_session_id=metadata.get("parent_session_id"),
    )
    entries: list[SessionEntry] = []
    parent_id = header.id
    for message in messages:
        entry = MessageEntry(
            id=new_entry_id(),
            parent_id=parent_id,
            timestamp=now_iso(),
            message=message_for_storage(message),
        )
        entries.append(entry)
        parent_id = entry.id
    JsonlSessionStore(root=target.parent).write_entries(target, header, entries)


def export_session(
    target: Path,
    *,
    source: Path | None,
    messages: list[LLMMessage],
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Export the current conversation to ``target``.

    ``.jsonl`` targets copy raw entries when ``source`` is a JSONL session and
    synthesize entries otherwise. ``.html`` targets render a minimal page and
    every other suffix is written as markdown.
    """
    metadata = metadata or {}
    match target.suffix.lower():
        case ".jsonl":
            if (
                source is not None
                and source.suffix == JSONL_SUFFIX
                and source.is_file()
            ):
                return export_raw_jsonl(source, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_synthetic_jsonl(target, messages, metadata)
            return target
        case ".html" | ".htm":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(export_html(messages, metadata), encoding="utf-8")
            return target
        case _:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(export_markdown(messages, metadata), encoding="utf-8")
            return target


def _unique_target(store: JsonlSessionStore, workdir: Path, session_id: str) -> Path:
    candidate = store.session_path(workdir, session_id)
    if not candidate.exists():
        return candidate
    for index in range(1, 100):
        alternative = candidate.with_name(f"{candidate.stem}-{index}{candidate.suffix}")
        if not alternative.exists():
            return alternative
    return candidate.with_name(
        f"{candidate.stem}-{new_entry_id()[:6]}{candidate.suffix}"
    )


def _stats_from_metadata(metadata: dict[str, Any]) -> AgentStats | None:
    raw = metadata.get("stats")
    if isinstance(raw, dict):
        try:
            return AgentStats.model_validate(raw)
        except Exception:
            return None
    return None


def _import_legacy(
    source: Path, store: JsonlSessionStore, workdir: Path, *, persist: bool
) -> ImportedSession:
    messages, metadata = InteractionLogger.load_session(source)
    session_id = str(metadata.get("session_id") or new_entry_id())
    if not persist:
        return ImportedSession(
            session_id=session_id,
            path=None,
            messages=messages,
            stats=_stats_from_metadata(metadata),
        )

    target = _unique_target(store, workdir, session_id)
    _write_synthetic_jsonl(target, messages, {**metadata, "session_id": session_id})
    return ImportedSession(
        session_id=session_id,
        path=target,
        messages=messages,
        stats=_stats_from_metadata(metadata),
    )


def import_session(
    source: Path, *, store: JsonlSessionStore, workdir: Path, persist: bool = True
) -> ImportedSession:
    """Load a ``.jsonl`` or legacy ``.json`` session for the running agent.

    When ``persist`` is true JSONL sessions are copied into the store root so
    subsequent turns append to the copy instead of mutating the user supplied
    file; otherwise the returned ``path`` is ``None``.

    Raises:
        ValueError: If the file has an unsupported suffix or cannot be parsed.
        OSError: If the session file exists but cannot be read or copied.
    """
    if not source.is_file():
        raise ValueError(f"Session file not found: {source}")

    if source.suffix == JSONL_SUFFIX:
        view = JsonlSessionStore(root=source.parent).read(source)
        messages = view.messages()
        if not persist:
            return ImportedSession(
                session_id=view.header.id,
                path=None,
                messages=messages,
                stats=view.stats(),
            )

        target = _unique_target(store, workdir, view.header.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return ImportedSession(
            session_id=view.header.id,
            path=target,
            messages=messages,
            stats=view.stats(),
        )

    if source.suffix == ".json":
        return _import_legacy(source, store, workdir, persist=persist)

    raise ValueError(f"Unsupported session file: {source}")
