from __future__ import annotations

from datetime import datetime
import getpass
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any

import aiofiles

from vibe.core.llm.format import get_active_tool_classes
from vibe.core.types import AgentStats, LLMMessage, SessionInfo, SessionMetadata
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


class InteractionLogger:
    def __init__(
        self,
        session_config: SessionLoggingConfig,
        session_id: str,
        auto_approve: bool = False,
        workdir: Path | None = None,
        existing_filepath: Path | None = None,
    ) -> None:
        if workdir is None:
            workdir = Path.cwd()
        self.session_config = session_config
        self.enabled = session_config.enabled
        self.auto_approve = auto_approve
        self.workdir = workdir

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
            self.session_metadata = self._load_existing_metadata(existing_filepath)
        else:
            self.filepath = self._get_save_filepath()
            self.session_metadata = self._initialize_session_metadata()

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
    ) -> str | None:
        if not self.enabled or self.filepath is None:
            return None

        if self.session_metadata is None:
            return None

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
            },
            "messages": [m.model_dump(exclude_none=True) for m in messages],
        }

        try:
            json_content = json.dumps(interaction_data, indent=2, ensure_ascii=False)

            async with aiofiles.open(self.filepath, "w", encoding="utf-8") as f:
                await f.write(json_content)

            return str(self.filepath)
        except Exception:
            return None

    def reset_session(self, session_id: str) -> None:
        if not self.enabled:
            return

        self.session_id = session_id
        self.session_start_time = datetime.now().isoformat()
        self.filepath = self._get_save_filepath()
        self.session_metadata = self._initialize_session_metadata()

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
        save_dir = Path(config.save_dir)
        if not save_dir.exists():
            return None

        pattern = f"{config.session_prefix}_*.json"
        session_files = list(save_dir.glob(pattern))

        if not session_files:
            return None

        return max(session_files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def find_session_by_id(
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
                return (
                    max(matches, key=lambda p: p.stat().st_mtime)
                    if len(matches) > 1
                    else matches[0]
                )

        return None

    @staticmethod
    def list_sessions(
        config: SessionLoggingConfig,
        limit: int = 20,
        workdir: Path | None = None,
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
        save_dir = Path(config.save_dir)
        if not save_dir.exists():
            return []

        pattern = f"{config.session_prefix}_*.json"
        session_files = list(save_dir.glob(pattern))

        if not session_files:
            return []

        # Sort by modification time, most recent first
        session_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        # Resolve workdir for comparison if provided
        resolved_workdir = workdir.resolve() if workdir else None

        results: list[tuple[Path, dict[str, Any]]] = []
        for filepath in session_files:
            if len(results) >= limit:
                break

            try:
                with filepath.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                metadata = data.get("metadata", {})

                # Filter by workdir if specified
                if resolved_workdir and not _matches_workdir(metadata, resolved_workdir):
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
                    "end_time": metadata.get("end_time", metadata.get("start_time", "")),
                    "last_user_message": last_user_message,
                }
                results.append((filepath, summary))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # Skip corrupted or unreadable files
                continue

        return results

    @staticmethod
    def load_session(filepath: Path) -> tuple[list[LLMMessage], dict[str, Any]]:
        with filepath.open("r", encoding="utf-8") as f:
            content = f.read()

        data = json.loads(content)
        messages = [LLMMessage.model_validate(msg) for msg in data.get("messages", [])]
        metadata = data.get("metadata", {})

        return messages, metadata
