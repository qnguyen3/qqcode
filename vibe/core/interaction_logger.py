from __future__ import annotations

from datetime import datetime
import getpass
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any, Literal

import aiofiles

from vibe.core.llm.format import get_active_tool_classes
from vibe.core.sessions.entries import (
    CustomEntry,
    MessageEntry,
    SessionEntry,
    SessionHeader,
    ThinkingLevelChangeEntry,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JSONL_SUFFIX, JsonlSessionStore
from vibe.core.types import (
    AgentStats,
    LLMMessage,
    SessionInfo,
    SessionMetadata,
    message_for_storage,
)
from vibe.core.utils import is_windows

if TYPE_CHECKING:
    from vibe.core.config import SessionLoggingConfig, VibeConfig
    from vibe.core.tools.manager import ToolManager


def _matches_workdir(metadata: dict[str, Any], resolved_workdir: Path) -> bool:
    """Check if session metadata matches the given working directory."""
    session_workdir = metadata.get("environment", {}).get("working_directory", "")
    if not session_workdir:
        return True  # Include sessions without workdir info
    try:
        return Path(session_workdir).resolve() == resolved_workdir
    except (OSError, ValueError):
        return False


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class InteractionLogger:
    """Facade over the append-only JSONL store.

    Keeps the historical API for legacy ``.json`` sessions while new
    sessions are written incrementally to ``.jsonl`` files.
    """

    def __init__(
        self,
        session_config: SessionLoggingConfig,
        session_id: str,
        auto_approve: bool = False,
        workdir: Path | None = None,
        existing_filepath: Path | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        if workdir is None:
            workdir = Path.cwd()
        self.session_config = session_config
        self.enabled = session_config.enabled
        self.auto_approve = auto_approve
        self.workdir = workdir
        self.parent_session_id = parent_session_id
        self.format: Literal["json", "jsonl"] = session_config.format
        self._write_format: Literal["json", "jsonl"] = session_config.format
        self._store = JsonlSessionStore(session_config)
        self._last_entry_id: str | None = None
        self._header: SessionHeader | None = None
        self._header_written = False

        if not self.enabled:
            self.save_dir: Path | None = None
            self.session_prefix: str | None = None
            self.session_id: str = "disabled"
            self.session_start_time: str = "N/A"
            self.filepath: Path | None = None
            self.session_metadata: SessionMetadata | None = None
            return

        self.save_dir = Path(session_config.save_dir)
        self.session_prefix = session_config.session_prefix
        self.session_id = session_id
        self.session_start_time = datetime.now().isoformat()

        self.save_dir.mkdir(parents=True, exist_ok=True)

        # Use existing session file if resuming, otherwise create new
        if existing_filepath and existing_filepath.exists():
            self.filepath = existing_filepath
            self._write_format = (
                "json" if existing_filepath.suffix == ".json" else "jsonl"
            )
            if self._write_format == "jsonl":
                self._load_jsonl_state(existing_filepath)
            else:
                self.session_metadata = self._load_existing_metadata(existing_filepath)
        elif self.format == "jsonl":
            self._write_format = "jsonl"
            self._header = self._build_header(self.session_id)
            self.filepath = self._store.session_path(self.workdir, self.session_id)
            self.session_metadata = self._initialize_session_metadata()
        else:
            self._write_format = "json"
            self.filepath = self._get_save_filepath()
            self.session_metadata = self._initialize_session_metadata()

    def _load_jsonl_state(self, filepath: Path) -> None:
        view = self._store.read(filepath)
        self._header = view.header
        self._header_written = True
        self._last_entry_id = view.entries[-1].id if view.entries else view.header.id
        self.session_metadata = SessionMetadata(
            session_id=self.session_id,
            start_time=view.header.timestamp,
            end_time=None,
            git_commit=view.header.git_commit,
            git_branch=view.header.git_branch,
            auto_approve=self.auto_approve,
            username=view.header.username or self._get_username(),
            environment={"working_directory": view.header.cwd or str(self.workdir)},
        )

    def _build_header(self, session_id: str) -> SessionHeader:
        return SessionHeader(
            id=session_id,
            timestamp=now_iso(),
            cwd=str(self.workdir),
            git_branch=self._get_git_branch(),
            git_commit=self._get_git_commit(),
            username=self._get_username(),
            parent_session_id=self.parent_session_id,
        )

    def _ensure_session_file(self) -> None:
        if self._header_written or self.filepath is None or self._header is None:
            return
        self._store.write_entries(self.filepath, self._header, [])
        self._header_written = True
        self._last_entry_id = self._header.id

    def _append_entry(self, entry: SessionEntry) -> str | None:
        if not self.enabled or self.filepath is None:
            return None
        self._ensure_session_file()
        self._store.append(entry, self.filepath)
        self._last_entry_id = entry.id
        return entry.id

    @property
    def writes_entries(self) -> bool:
        """Whether entries are appended to a ``.jsonl`` session file."""
        return (
            self.enabled and self._write_format == "jsonl" and self.filepath is not None
        )

    def append_entry(self, entry: SessionEntry) -> str | None:
        """Persist ``entry`` as the next line of the session file.

        The entry keeps the id the agent assigned, so the file and the in-memory
        context share identities. Image bytes are dropped on the way out.
        Returns ``None`` when nothing is written (logging disabled or legacy
        ``.json`` format).
        """
        if self._write_format != "jsonl":
            return None
        if isinstance(entry, MessageEntry):
            stored = message_for_storage(entry.message)
            entry = entry.model_copy(update={"message": stored})
        return self._append_entry(entry)

    def append_message(
        self, message: LLMMessage, parent_id: str | None = None
    ) -> str | None:
        return self.append_entry(
            MessageEntry(
                id=new_entry_id(),
                parent_id=parent_id if parent_id is not None else self._last_entry_id,
                timestamp=now_iso(),
                message=message,
            )
        )

    def _get_save_filepath(self) -> Path:
        if self.save_dir is None or self.session_prefix is None:
            raise RuntimeError("Cannot get filepath when logging is disabled")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.session_prefix}_{timestamp}_{self.session_id[:8]}.json"
        return self.save_dir / filename

    def _get_git_commit(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                cwd=self.workdir,
                stdin=subprocess.DEVNULL if is_windows() else None,
                text=True,
                timeout=5.0,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.strip()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        return None

    def _get_git_branch(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                cwd=self.workdir,
                stdin=subprocess.DEVNULL if is_windows() else None,
                text=True,
                timeout=5.0,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.strip()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        return None

    def _get_username(self) -> str:
        try:
            return getpass.getuser()
        except Exception:
            return "unknown"

    def _initialize_session_metadata(self) -> SessionMetadata:
        git_commit = self._get_git_commit()
        git_branch = self._get_git_branch()
        user_name = self._get_username()

        return SessionMetadata(
            session_id=self.session_id,
            start_time=self.session_start_time,
            end_time=None,
            git_commit=git_commit,
            git_branch=git_branch,
            auto_approve=self.auto_approve,
            username=user_name,
            environment={"working_directory": str(self.workdir)},
        )

    def _load_existing_metadata(self, filepath: Path) -> SessionMetadata:
        """Load and update existing session metadata when resuming a session."""
        try:
            with filepath.open("r", encoding="utf-8") as f:
                data = json.load(f)
            metadata = data.get("metadata", {})
            # Keep original start_time, update other fields as needed
            return SessionMetadata(
                session_id=self.session_id,
                start_time=metadata.get("start_time", self.session_start_time),
                end_time=None,
                git_commit=self._get_git_commit() or metadata.get("git_commit"),
                git_branch=self._get_git_branch() or metadata.get("git_branch"),
                auto_approve=self.auto_approve,
                username=self._get_username(),
                environment={"working_directory": str(self.workdir)},
            )
        except (OSError, json.JSONDecodeError):
            # If we can't load existing metadata, create fresh metadata
            return self._initialize_session_metadata()

    async def save_interaction(
        self,
        messages: list[LLMMessage],
        stats: AgentStats,
        config: VibeConfig,
        tool_manager: ToolManager,
        additional_directory_contexts: list[Path] | None = None,
    ) -> str | None:
        if not self.enabled or self.filepath is None:
            return None

        if self.session_metadata is None:
            return None

        if self._write_format == "jsonl":
            return str(self.filepath)

        active_tools = get_active_tool_classes(tool_manager, config)

        tools_available = [
            {
                "type": "function",
                "function": {
                    "name": tool_class.get_name(),
                    "description": tool_class.description,
                    "parameters": tool_class.get_parameters(),
                },
            }
            for tool_class in active_tools
        ]

        interaction_data = {
            "metadata": {
                **self.session_metadata.model_dump(),
                "end_time": datetime.now().isoformat(),
                "stats": stats.model_dump(),
                "total_messages": len(messages),
                "tools_available": tools_available,
                "agent_config": config.model_dump(mode="json"),
                "additional_directory_contexts": [
                    str(path) for path in additional_directory_contexts
                ]
                if additional_directory_contexts
                else [],
            },
            "messages": [
                message_for_storage(m).model_dump(exclude_none=True) for m in messages
            ],
        }

        try:
            json_content = json.dumps(interaction_data, indent=2, ensure_ascii=False)

            async with aiofiles.open(self.filepath, "w", encoding="utf-8") as f:
                await f.write(json_content)

            return str(self.filepath)
        except Exception:
            return None

    def get_session_info(
        self, messages: list[dict[str, Any]], stats: AgentStats
    ) -> SessionInfo:
        if not self.enabled or self.save_dir is None:
            return SessionInfo(
                session_id="disabled",
                start_time="N/A",
                message_count=len(messages),
                stats=stats,
                save_dir="N/A",
            )

        return SessionInfo(
            session_id=self.session_id,
            start_time=self.session_start_time,
            message_count=len(messages),
            stats=stats,
            save_dir=str(self.save_dir),
        )

    @staticmethod
    def find_latest_session(config: SessionLoggingConfig) -> Path | None:
        candidates: list[Path] = []
        store = JsonlSessionStore(config)
        if store.root.exists():
            candidates.extend(store.root.glob(f"*/*{JSONL_SUFFIX}"))

        save_dir = Path(config.save_dir)
        if save_dir.exists():
            candidates.extend(save_dir.glob(f"{config.session_prefix}_*.json"))

        if not candidates:
            return None

        return max(candidates, key=_safe_mtime)

    @staticmethod
    def find_session_by_id(
        session_id: str, config: SessionLoggingConfig
    ) -> Path | None:
        matches: list[Path] = []
        if jsonl_path := JsonlSessionStore(config).find(session_id):
            matches.append(jsonl_path)
        if legacy_path := InteractionLogger._find_legacy_by_id(session_id, config):
            matches.append(legacy_path)
        if not matches:
            return None
        return max(matches, key=_safe_mtime)

    @staticmethod
    def _find_legacy_by_id(
        session_id: str, config: SessionLoggingConfig
    ) -> Path | None:
        save_dir = Path(config.save_dir)
        if not save_dir.exists():
            return None

        # If it's a full UUID, extract the short form (first 8 chars)
        short_id = session_id.split("-")[0] if "-" in session_id else session_id

        # Try exact match first, then partial
        patterns = [
            f"{config.session_prefix}_*_{short_id}.json",  # Exact short UUID
            f"{config.session_prefix}_*_{short_id}*.json",  # Partial UUID
        ]

        for pattern in patterns:
            matches = list(save_dir.glob(pattern))
            if matches:
                return max(matches, key=_safe_mtime)
        return None

    @staticmethod
    def list_sessions(
        config: SessionLoggingConfig, limit: int = 20, workdir: Path | None = None
    ) -> list[tuple[Path, dict[str, Any]]]:
        """List recent sessions with summary info, sorted by most recent first.

        Args:
            config: Session logging configuration
            limit: Maximum number of sessions to return
            workdir: If provided, only return sessions from this working directory

        Returns:
            List of (filepath, summary_dict) tuples where summary_dict contains:
            - session_id: Short session ID (8 chars)
            - end_time: Session end time for display
            - last_user_message: Last user message content (for preview)
        """
        results: list[tuple[Path, dict[str, Any]]] = []
        store = JsonlSessionStore(config)
        for summary in store.list(workdir=workdir, limit=limit):
            results.append((
                summary.path,
                {
                    "session_id": summary.session_id[:8],
                    "end_time": summary.end_time,
                    "last_user_message": summary.last_user_message,
                    "start_time": summary.start_time,
                },
            ))
        results.extend(InteractionLogger._list_legacy_sessions(config, workdir))

        results.sort(key=lambda item: _safe_mtime(item[0]), reverse=True)
        return results[:limit]

    @staticmethod
    def _list_legacy_sessions(
        config: SessionLoggingConfig, workdir: Path | None
    ) -> list[tuple[Path, dict[str, Any]]]:
        save_dir = Path(config.save_dir)
        if not save_dir.exists():
            return []

        pattern = f"{config.session_prefix}_*.json"
        session_files = list(save_dir.glob(pattern))

        if not session_files:
            return []

        session_files.sort(key=_safe_mtime, reverse=True)

        resolved_workdir = workdir.resolve() if workdir else None

        results: list[tuple[Path, dict[str, Any]]] = []
        for filepath in session_files:
            try:
                with filepath.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                metadata = data.get("metadata", {})

                # Filter by workdir if specified
                if resolved_workdir and not _matches_workdir(
                    metadata, resolved_workdir
                ):
                    continue

                messages = data.get("messages", [])

                # Find last user message
                last_user_message = ""
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        last_user_message = str(msg.get("content", ""))[:100]
                        break

                summary = {
                    "session_id": metadata.get("session_id", "unknown")[:8],
                    "end_time": metadata.get(
                        "end_time", metadata.get("start_time", "")
                    ),
                    "last_user_message": last_user_message,
                }
                results.append((filepath, summary))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # Skip corrupted or unreadable files
                continue

        return results

    @staticmethod
    def load_session(filepath: Path) -> tuple[list[LLMMessage], dict[str, Any]]:
        if filepath.suffix == JSONL_SUFFIX:
            store = JsonlSessionStore(root=filepath.parent)
            view = store.read(filepath)
            messages = view.messages()
            custom: dict[str, Any] = {
                entry.key: entry.value
                for entry in view.entries
                if isinstance(entry, CustomEntry)
            }
            last_model = view.last_model()
            last_thinking = next(
                (
                    entry.level
                    for entry in reversed(view.entries)
                    if isinstance(entry, ThinkingLevelChangeEntry)
                ),
                None,
            )
            metadata: dict[str, Any] = {
                "session_id": view.header.id,
                "start_time": view.header.timestamp,
                "end_time": (
                    view.entries[-1].timestamp
                    if view.entries
                    else view.header.timestamp
                ),
                "git_branch": view.header.git_branch,
                "git_commit": view.header.git_commit,
                "cwd": view.header.cwd,
                "parent_session_id": view.header.parent_session_id,
                "total_messages": len(messages),
                "stats": view.stats().model_dump(mode="json"),
                "truncated_at": view.truncated_at,
                "custom": custom,
                "model": last_model.model_alias if last_model else None,
                "thinking_level": last_thinking,
            }
            return messages, metadata

        with filepath.open("r", encoding="utf-8") as f:
            content = f.read()

        data = json.loads(content)
        messages = [LLMMessage.model_validate(msg) for msg in data.get("messages", [])]
        metadata = data.get("metadata", {})

        return messages, metadata
