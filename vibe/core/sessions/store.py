from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Protocol

from pydantic import BaseModel

from vibe.core.config import SessionLoggingConfig, get_vibe_home
from vibe.core.sessions.entries import (
    CompactionEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionHeader,
    UsageEntry,
    new_entry_id,
    now_iso,
    parse_entry,
)
from vibe.core.types import AgentStats, LLMMessage, Role
from vibe.core.utils import logger

JSONL_SUFFIX = ".jsonl"
_NO_SYSTEM_MESSAGE = LLMMessage(role=Role.system, content="")


class SessionEntryNotFoundError(LookupError):
    def __init__(self, entry_id: str) -> None:
        super().__init__(f"Entry '{entry_id}' not found in session")
        self.entry_id = entry_id


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    path: Path
    start_time: str
    end_time: str
    message_count: int
    last_user_message: str
    cwd: str = ""
    parent_session_id: str | None = None
    modified_at: float = 0.0


class SessionUsageStats(BaseModel):
    """Aggregated usage for one session, derived from ``UsageEntry`` records."""

    session_id: str
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    message_count: int = 0
    compactions: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class SessionView:
    header: SessionHeader
    entries: list[SessionEntry]
    truncated_at: int | None = None

    def messages(self, *, include_compacted: bool = False) -> list[LLMMessage]:
        """Return the messages of the current context, without the system prompt.

        Head markers, compactions and context edits are applied, so the result
        matches what the model would see if the session continued. With
        ``include_compacted`` every stored message is returned instead.
        """
        if include_compacted:
            return [
                entry.message
                for entry in self.entries
                if isinstance(entry, MessageEntry)
            ]

        # context.builder imports the entry models from this package, so the
        # import is deferred to avoid a cycle at module load.
        from vibe.core.context.builder import ContextBuilder

        return ContextBuilder(_NO_SYSTEM_MESSAGE).build(self.entries).messages[1:]

    def stats(self) -> AgentStats:
        stats = AgentStats()
        for entry in self.entries:
            if isinstance(entry, UsageEntry):
                stats.session_prompt_tokens += entry.prompt_tokens
                stats.session_completion_tokens += entry.completion_tokens
                stats.steps += 1
        return stats

    def usage_stats(self) -> SessionUsageStats:
        """Aggregate every ``UsageEntry`` plus message/compaction counts."""
        result = SessionUsageStats(session_id=self.header.id)
        for entry in self.entries:
            match entry:
                case UsageEntry():
                    result.turns += 1
                    result.prompt_tokens += entry.prompt_tokens
                    result.completion_tokens += entry.completion_tokens
                    result.cache_read_tokens += entry.cache_read_tokens
                    result.cache_write_tokens += entry.cache_write_tokens
                    result.cost_usd = round(result.cost_usd + entry.cost_usd, 6)
                case MessageEntry():
                    result.message_count += 1
                case CompactionEntry():
                    result.compactions += 1
                case ModelChangeEntry():
                    result.model = entry.model_alias
        return result

    def last_model(self) -> ModelChangeEntry | None:
        for entry in reversed(self.entries):
            if isinstance(entry, ModelChangeEntry):
                return entry
        return None

    def fork_point(self, parent_id: str | None) -> SessionView:
        if parent_id is None:
            return self
        for index, entry in enumerate(self.entries):
            if entry.id == parent_id:
                return SessionView(
                    header=self.header,
                    entries=list(self.entries[: index + 1]),
                    truncated_at=self.truncated_at,
                )
        raise SessionEntryNotFoundError(parent_id)


@dataclass(frozen=True)
class RepairResult:
    path: Path
    kept_entries: int
    removed_bytes: int
    backup_path: Path | None = None


@dataclass(frozen=True)
class SessionTail:
    """Entries written after a known entry, read back from a session file."""

    entries: list[SessionEntry]
    anchor_found: bool


def _matches_entry(line: bytes, entry_id: str) -> bool:
    try:
        return parse_entry(line).id == entry_id
    except ValueError:
        return False


class SessionStore(Protocol):
    def append(self, entry: SessionEntry) -> None: ...

    def read(self, path: Path) -> SessionView: ...

    def list(self, *, workdir: Path | None, limit: int) -> list[SessionSummary]: ...

    def find(self, session_id: str) -> Path | None: ...

    def fork(self, source: Path, at_entry_id: str, *, workdir: Path) -> Path: ...


def project_dir_name(workdir: Path) -> str:
    resolved = Path(workdir).expanduser().resolve()
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", resolved.name).strip("-")[:40]
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'root'}-{digest}"


def _safe_id(session_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "", session_id)
    return cleaned or "session"


class JsonlSessionStore:
    """Append-only JSONL session store.

    Each append writes a single JSON object per line and flushes (and
    optionally fsyncs) so a crash can only damage the last line.
    """

    def __init__(
        self, config: SessionLoggingConfig | None = None, *, root: Path | None = None
    ) -> None:
        if root is not None:
            self._root = Path(root).expanduser()
        elif config is not None and config.sessions_dir:
            self._root = Path(config.sessions_dir).expanduser()
        else:
            self._root = get_vibe_home() / "sessions"
        self.fsync = config.fsync if config is not None else True
        self._current_path: Path | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def current_path(self) -> Path | None:
        return self._current_path

    def session_path(
        self, workdir: Path, session_id: str, *, timestamp: datetime | None = None
    ) -> Path:
        stamp = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
        return (
            self._root
            / project_dir_name(workdir)
            / f"{stamp}_{_safe_id(session_id)[:8]}{JSONL_SUFFIX}"
        )

    def write_entries(
        self, path: Path, header: SessionHeader, entries: list[SessionEntry]
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(f"{header.model_dump_json()}\n")
            for entry in entries:
                handle.write(f"{entry.model_dump_json()}\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self._current_path = path

    def append(self, entry: SessionEntry, path: Path | None = None) -> None:
        target = path or self._current_path
        if target is None:
            raise ValueError("No session path provided and no current session set")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{entry.model_dump_json()}\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self._current_path = target

    def read(self, path: Path) -> SessionView:
        entries, truncated_at = self._scan(path)
        header: SessionHeader | None = None
        if entries and isinstance(entries[0], SessionHeader):
            header = entries[0]
            entries = entries[1:]
        else:
            header = SessionHeader(
                id=f"corrupt-{path.stem}", timestamp=now_iso(), cwd=""
            )
        return SessionView(header=header, entries=entries, truncated_at=truncated_at)

    def read_since(self, path: Path, after_entry_id: str | None) -> SessionTail:
        """Return the entries appended after ``after_entry_id``.

        ``None`` returns every entry. When the anchor is missing, for example
        because the file was rewritten or repaired, ``anchor_found`` is false
        and no entries are returned, so the caller keeps what it already has.
        Lines before the anchor are only scanned, not parsed.
        """
        try:
            data = path.read_bytes()
        except OSError:
            return SessionTail(entries=[], anchor_found=after_entry_id is None)

        anchored = after_entry_id is None
        needle = b"" if after_entry_id is None else f'"id":"{after_entry_id}"'.encode()
        entries: list[SessionEntry] = []
        for line in data.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not anchored:
                anchored = (
                    after_entry_id is not None
                    and needle in stripped
                    and _matches_entry(stripped, after_entry_id)
                )
                continue
            try:
                entry = parse_entry(stripped)
            except ValueError:
                break
            if not isinstance(entry, SessionHeader):
                entries.append(entry)
        return SessionTail(entries=entries, anchor_found=anchored)

    def list(
        self, *, workdir: Path | None = None, limit: int = 20
    ) -> list[SessionSummary]:
        if not self._root.exists():
            return []
        if workdir is not None:
            directories = [self._root / project_dir_name(workdir)]
        else:
            directories = [entry for entry in self._root.iterdir() if entry.is_dir()]

        summaries: list[SessionSummary] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.glob(f"*{JSONL_SUFFIX}"):
                if summary := self._summarize(path):
                    summaries.append(summary)

        summaries.sort(key=lambda summary: summary.modified_at, reverse=True)
        return summaries[:limit] if limit >= 0 else summaries

    def find(self, session_id: str) -> Path | None:
        if not self._root.exists():
            return None
        needle = session_id.strip().lower()
        if not needle:
            return None
        matches = [
            path
            for path in self._root.glob(f"*/*{JSONL_SUFFIX}")
            if _id_matches(path.stem, needle)
        ]
        if not matches:
            return None
        return max(matches, key=_safe_mtime)

    def fork(self, source: Path, at_entry_id: str, *, workdir: Path) -> Path:
        view = self.read(source)
        forked = view.fork_point(at_entry_id)
        header = SessionHeader(
            id=new_entry_id(),
            timestamp=now_iso(),
            cwd=str(workdir),
            git_branch=view.header.git_branch,
            git_commit=view.header.git_commit,
            username=view.header.username,
            parent_session_id=view.header.id,
        )
        path = self.session_path(workdir, header.id)
        self.write_entries(path, header, forked.entries)
        return path

    def repair(self, path: Path) -> RepairResult:
        entries, truncated_at = self._scan(path)
        if truncated_at is None:
            return RepairResult(path=path, kept_entries=len(entries), removed_bytes=0)

        original = path.read_bytes()
        backup = path.with_name(f"{path.name}.bak")
        shutil.copy2(path, backup)
        with path.open("wb") as handle:
            handle.write(original[:truncated_at])
            handle.flush()
            os.fsync(handle.fileno())
        logger.warning(
            "Repaired session %s: removed %d bytes, backup at %s",
            path,
            len(original) - truncated_at,
            backup,
        )
        return RepairResult(
            path=path,
            kept_entries=len(entries),
            removed_bytes=len(original) - truncated_at,
            backup_path=backup,
        )

    def _scan(self, path: Path) -> tuple[list[SessionEntry], int | None]:
        try:
            data = path.read_bytes()
        except OSError:
            return [], None

        entries: list[SessionEntry] = []
        truncated_at: int | None = None
        offset = 0
        for line in data.splitlines(keepends=True):
            if stripped := line.strip():
                try:
                    entries.append(parse_entry(stripped))
                except ValueError:
                    truncated_at = offset
                    break
            offset += len(line)
        return entries, truncated_at

    def _summarize(self, path: Path) -> SessionSummary | None:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return None

        view = self.read(path)
        messages = view.messages(include_compacted=True)
        last_user_message = next(
            (
                message.content or ""
                for message in reversed(messages)
                if message.role is Role.user
            ),
            "",
        )
        end_time = view.entries[-1].timestamp if view.entries else view.header.timestamp
        return SessionSummary(
            session_id=view.header.id,
            path=path,
            start_time=view.header.timestamp,
            end_time=end_time,
            message_count=sum(
                1 for entry in view.entries if isinstance(entry, MessageEntry)
            ),
            last_user_message=last_user_message[:100],
            cwd=view.header.cwd,
            parent_session_id=view.header.parent_session_id,
            modified_at=modified_at,
        )


def _id_matches(stem: str, needle: str) -> bool:
    id_part = stem.rsplit("_", 1)[-1].lower()
    if not id_part:
        return False
    return id_part.startswith(needle[:8]) or needle.startswith(id_part)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
