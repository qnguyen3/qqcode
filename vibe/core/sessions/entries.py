from __future__ import annotations

from datetime import UTC, datetime
import os
import threading
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from vibe.core.types import LLMMessage

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_TIME_CHARS = 10
_ULID_RANDOM_CHARS = 16
_ULID_RANDOM_BITS = 80

_id_lock = threading.Lock()
_last_ulid_ms = -1
_last_ulid_random = 0


def _encode_ulid_part(value: int, length: int) -> str:
    return "".join(
        _ULID_ALPHABET[(value >> (5 * (length - 1 - index))) & 0x1F]
        for index in range(length)
    )


def new_entry_id(*, timestamp_ms: int | None = None) -> str:
    """Return a lexicographically time-sortable, unique ULID string."""
    global _last_ulid_ms, _last_ulid_random
    millis = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    with _id_lock:
        if millis > _last_ulid_ms:
            _last_ulid_ms = millis
            _last_ulid_random = int.from_bytes(os.urandom(10), "big")
        else:
            millis = _last_ulid_ms
            _last_ulid_random += 1
            if _last_ulid_random.bit_length() > _ULID_RANDOM_BITS:
                _last_ulid_ms += 1
                _last_ulid_random = int.from_bytes(os.urandom(10), "big")
        random_part = _last_ulid_random
    return _encode_ulid_part(millis, _ULID_TIME_CHARS) + _encode_ulid_part(
        random_part, _ULID_RANDOM_CHARS
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SessionEntryBase(BaseModel):
    id: str
    parent_id: str | None = None
    timestamp: str


class SessionHeader(SessionEntryBase):
    type: Literal["session"] = "session"
    version: int = 1
    cwd: str
    git_branch: str | None = None
    git_commit: str | None = None
    username: str | None = None
    parent_session_id: str | None = None


class MessageEntry(SessionEntryBase):
    type: Literal["message"] = "message"
    message: LLMMessage


class CompactionEntry(SessionEntryBase):
    type: Literal["compaction"] = "compaction"
    first_kept_id: str | None
    summary: str
    tokens_before: int
    tokens_after: int
    is_update: bool = False


class ModelChangeEntry(SessionEntryBase):
    type: Literal["model_change"] = "model_change"
    model_alias: str
    provider: str


class ThinkingLevelChangeEntry(SessionEntryBase):
    type: Literal["thinking_level_change"] = "thinking_level_change"
    level: str | None = None


class UsageEntry(SessionEntryBase):
    type: Literal["usage"] = "usage"
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class HeadMarkerEntry(SessionEntryBase):
    """Marks a new context head.

    Older entries stay in the log but leave the context. ``keep_from_id`` keeps
    the conversation from that entry onwards; ``None`` keeps only the entries
    appended after the marker.
    """

    type: Literal["head_marker"] = "head_marker"
    keep_from_id: str | None = None
    reason: str | None = None


class ContextEditEntry(SessionEntryBase):
    """Removes (``omit``) or rewrites (``replace``) one message in the context.

    The stored log is not touched. Later edits win over earlier ones.
    """

    type: Literal["context_edit"] = "context_edit"
    target_id: str
    action: Literal["omit", "replace"] = "omit"
    replacement: str | None = None


class CustomEntry(SessionEntryBase):
    type: Literal["custom"] = "custom"
    key: str
    value: dict[str, Any]


SessionEntry = Annotated[
    SessionHeader
    | MessageEntry
    | CompactionEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | UsageEntry
    | CustomEntry
    | HeadMarkerEntry
    | ContextEditEntry,
    Field(discriminator="type"),
]

SESSION_ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)

SUMMARY_TAG = "conversation_summary"


def parse_entry(data: bytes | str | dict[str, Any]) -> SessionEntry:
    if isinstance(data, bytes | str):
        return SESSION_ENTRY_ADAPTER.validate_json(data)
    return SESSION_ENTRY_ADAPTER.validate_python(data)
