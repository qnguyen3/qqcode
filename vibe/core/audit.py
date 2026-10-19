from __future__ import annotations

from enum import StrEnum, auto
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vibe.core.config import AuditConfig

logger = logging.getLogger(__name__)

SENSITIVE_TOOLS = frozenset({"bash", "read_file", "write_file", "edit"})
SUMMARY_MAX_CHARS = 200
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP_FILES = 3


class AuditDecision(StrEnum):
    """Permission outcome recorded for a tool call."""

    APPROVED = auto()
    DENIED = auto()
    DENYLIST = auto()


def _args_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summarize_args(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return a short, non-secret summary for sensitive tools."""
    if tool_name not in SENSITIVE_TOOLS:
        return None

    value: Any = None
    match tool_name:
        case "bash":
            value = args.get("command")
        case "read_file" | "write_file":
            value = args.get("path")
        case "edit":
            value = args.get("file_path")
        case _:
            value = None

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\n", " ")
    if len(text) > SUMMARY_MAX_CHARS:
        text = f"{text[:SUMMARY_MAX_CHARS]}..."
    return text


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return os.getenv("USER", "unknown")


class AuditLogger:
    """Append-only JSONL audit log with size-based rotation.

    Each record documents one tool permission decision. Full arguments are
    only stored when ``log_args`` is enabled; sensitive tools always include a
    truncated command/path plus an arguments hash instead.
    """

    def __init__(
        self,
        path: Path,
        *,
        log_args: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_files: int = DEFAULT_KEEP_FILES,
        session_id: str = "",
        user: str | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.log_args = log_args
        self.max_bytes = max_bytes
        self.keep_files = max(1, keep_files)
        self.session_id = session_id
        self.user = user or _current_user()
        self._lock = threading.Lock()

    def record(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: AuditDecision,
        *,
        mode: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one audit record and return the stored mapping."""
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_id": session_id if session_id is not None else self.session_id,
            "tool": tool_name,
            "args_hash": _args_hash(args),
            "decision": decision.value,
            "mode": mode,
            "user": self.user,
        }
        if self.log_args:
            entry["args"] = args
        elif (summary := _summarize_args(tool_name, args)) is not None:
            entry["args_summary"] = summary

        self._append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{line}\n")
                    handle.flush()
            except OSError as exc:
                logger.warning("Could not write audit log %s: %s", self.path, exc)

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return

        backups = self.keep_files - 1
        if backups <= 0:
            self.path.unlink(missing_ok=True)
            return

        oldest = self.path.with_name(f"{self.path.name}.{backups}")
        oldest.unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))


def build_audit_logger(
    config: AuditConfig, *, session_id: str = "", user: str | None = None
) -> AuditLogger | None:
    """Create an :class:`AuditLogger` when audit logging is enabled."""
    if not config.enabled:
        return None
    return AuditLogger(
        Path(config.log_path),
        log_args=config.log_args,
        max_bytes=config.max_bytes,
        keep_files=config.keep_files,
        session_id=session_id,
        user=user,
    )
