"""Derive request messages from the session entry log.

The entry log is the single source of truth for the visible context: messages
are replayed from entries, cut by head markers and compactions, and adjusted
by context edits. Tool calls that lost their responses in the middle of the
history get placeholder responses, so the context stays well formed. Delta
rebuilds only replay the entries appended since the previous build.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from vibe.core.sessions.entries import (
    SUMMARY_TAG,
    CompactionEntry,
    ContextEditEntry,
    HeadMarkerEntry,
    MessageEntry,
    SessionEntry,
)
from vibe.core.types import LLMMessage, Role, ToolCall
from vibe.core.utils import CancellationReason, get_user_cancellation_message

type EditAction = Literal["omit", "replace"]


@dataclass(frozen=True)
class ContextEdit:
    """The effect of the latest ``ContextEditEntry`` on one message."""

    action: EditAction
    replacement: str | None = None


@dataclass(frozen=True)
class BuildResult:
    """Context messages plus the state needed for incremental rebuilds.

    ``entry_ids`` is aligned with ``messages``; synthetic messages (the system
    prompt, placeholders for missing tool responses) carry ``None``.
    """

    messages: list[LLMMessage]
    last_entry_id: str | None
    reused_from: str | None = None
    entry_ids: list[str | None] = field(default_factory=list)
    edits: dict[str, ContextEdit] = field(default_factory=dict)

    def entry_id_at(self, index: int) -> str | None:
        if 0 <= index < len(self.entry_ids):
            return self.entry_ids[index]
        return None


@dataclass(frozen=True)
class ContextOverview:
    """A snapshot of what the model sees, for the ``/context`` command."""

    messages: int
    entries: int
    head_marker: HeadMarkerEntry | None
    omitted: int
    replaced: int
    recent_entries: list[SessionEntry]

    def describe(self) -> str:
        lines = [
            f"**Context**: {self.messages} message(s) from {self.entries} log entries",
            f"**Context edits**: {self.omitted} omitted, {self.replaced} replaced",
        ]
        if self.head_marker is None:
            lines.append("**Head marker**: none")
        else:
            reason = self.head_marker.reason or "unspecified"
            lines.append(f"**Head marker**: {reason} at {self.head_marker.timestamp}")
        lines.append("**Recent entries**:")
        lines.extend(f"- `{_entry_label(entry)}`" for entry in self.recent_entries)
        return "\n".join(lines)


def _entry_label(entry: SessionEntry) -> str:
    match entry:
        case MessageEntry():
            return f"message {entry.message.role.value} {entry.id[-6:]}"
        case CompactionEntry():
            return f"compaction {entry.id[-6:]}"
        case HeadMarkerEntry():
            return f"head_marker {entry.id[-6:]}"
        case ContextEditEntry():
            return f"context_edit {entry.target_id[-6:]} {entry.action}"
        case _:
            return f"{entry.type} {entry.id[-6:]}"


def pending_tool_calls(messages: Sequence[LLMMessage]) -> list[ToolCall]:
    """Return the tool calls of the trailing assistant turn without a response."""
    answered = 0
    for message in reversed(messages):
        if message.role is Role.tool:
            answered += 1
        elif message.role is Role.assistant and message.tool_calls:
            return message.tool_calls[answered:]
        else:
            return []
    return []


def placeholder_response(call: ToolCall) -> LLMMessage:
    """Build the response recorded for a tool call that never produced one."""
    name = ""
    if call.function is not None:
        name = call.function.name or ""
    return LLMMessage(
        role=Role.tool,
        tool_call_id=call.id or "",
        name=name,
        content=str(get_user_cancellation_message(CancellationReason.TOOL_NO_RESPONSE)),
    )


def _summary_message(summary: str) -> LLMMessage:
    return LLMMessage(
        role=Role.user, content=f"<{SUMMARY_TAG}>\n{summary}\n</{SUMMARY_TAG}>"
    )


def _edit_map(entries: Sequence[SessionEntry]) -> dict[str, ContextEdit]:
    """Collect edits in log order so later edits win over earlier ones."""
    edits: dict[str, ContextEdit] = {}
    for entry in entries:
        if isinstance(entry, ContextEditEntry):
            edits[entry.target_id] = ContextEdit(
                action=entry.action, replacement=entry.replacement
            )
    return edits


def _visible_message(
    entry: MessageEntry, edits: dict[str, ContextEdit]
) -> tuple[LLMMessage, bool]:
    """Return the message as the model should see it and whether it is omitted."""
    edit = edits.get(entry.id)
    if edit is None:
        return entry.message, False
    if edit.action == "omit":
        return entry.message, True
    return entry.message.model_copy(update={"content": edit.replacement or ""}), False


def _position_of(entries: Sequence[SessionEntry], entry_id: str) -> int | None:
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].id == entry_id:
            return index
    return None


@dataclass
class _Fold:
    """Replayed messages before visibility is applied.

    Omitted messages stay in the fold, flagged hidden, so a later compaction or
    head marker can still cut at them by entry id.
    """

    messages: list[LLMMessage] = field(default_factory=list)
    entry_ids: list[str | None] = field(default_factory=list)
    hidden: list[bool] = field(default_factory=list)

    def cut_at(self, entry_id: str | None) -> _Fold:
        """Keep the fold from ``entry_id`` onwards; unknown ids keep nothing."""
        start = len(self.entry_ids)
        if entry_id is not None and entry_id in self.entry_ids:
            start = self.entry_ids.index(entry_id)
        return _Fold(
            messages=self.messages[start:],
            entry_ids=self.entry_ids[start:],
            hidden=self.hidden[start:],
        )

    def compacted(self, entry: CompactionEntry) -> _Fold:
        kept = self.cut_at(entry.first_kept_id)
        return _Fold(
            messages=[_summary_message(entry.summary), *kept.messages],
            entry_ids=[entry.id, *kept.entry_ids],
            hidden=[False, *kept.hidden],
        )


@dataclass
class _Output:
    """The messages sent to the model, each with the entry id behind it."""

    messages: list[LLMMessage]
    entry_ids: list[str | None]

    def emit(self, message: LLMMessage, entry_id: str | None) -> None:
        if message.role is not Role.tool:
            for call in pending_tool_calls(self.messages):
                self._append(placeholder_response(call), None)
        self._append(message, entry_id)

    def _append(self, message: LLMMessage, entry_id: str | None) -> None:
        self.messages.append(message)
        self.entry_ids.append(entry_id)


def _invalidates(batch: Sequence[SessionEntry], base: BuildResult) -> bool:
    """Whether appending ``batch`` to ``base`` requires a full rebuild."""
    batch_ids = {entry.id for entry in batch}
    base_ids: set[str] | None = None
    for entry in batch:
        match entry:
            case HeadMarkerEntry() | CompactionEntry():
                return True
            case ContextEditEntry(target_id=target) if target not in batch_ids:
                if base_ids is None:
                    base_ids = {entry_id for entry_id in base.entry_ids if entry_id}
                if target in base_ids:
                    return True
    return False


class ContextBuilder:
    """Rebuilds request messages from a session entry log."""

    def __init__(self, system_message: LLMMessage) -> None:
        self._system_message = system_message

    @property
    def system_message(self) -> LLMMessage:
        return self._system_message

    def build(self, entries: Sequence[SessionEntry]) -> BuildResult:
        """Replay every entry into a fresh context."""
        edits = _edit_map(entries)
        fold = _Fold()
        for entry in entries:
            match entry:
                case MessageEntry():
                    message, hidden = _visible_message(entry, edits)
                    fold.messages.append(message)
                    fold.entry_ids.append(entry.id)
                    fold.hidden.append(hidden)
                case HeadMarkerEntry():
                    fold = fold.cut_at(entry.keep_from_id)
                case CompactionEntry():
                    fold = fold.compacted(entry)

        output = _Output(messages=[self._system_message], entry_ids=[None])
        for message, entry_id, hidden in zip(
            fold.messages, fold.entry_ids, fold.hidden, strict=True
        ):
            if not hidden:
                output.emit(message, entry_id)
        return self._result(
            output,
            last_entry_id=entries[-1].id if entries else None,
            reused_from=None,
            edits=edits,
        )

    def build_from(
        self, entries: Sequence[SessionEntry], base: BuildResult | None
    ) -> BuildResult:
        """Continue from ``base`` when only plain entries were appended.

        Falls back to :meth:`build` when the base no longer matches the log: a
        new head marker or compaction, an edit that targets a message already
        replayed into the base, or a base that is not part of ``entries``.
        """
        if base is None or base.last_entry_id is None:
            return self.build(entries)
        position = _position_of(entries, base.last_entry_id)
        if position is None:
            return self.build(entries)

        batch = entries[position + 1 :]
        if _invalidates(batch, base):
            return self.build(entries)

        edits = {**base.edits, **_edit_map(batch)}
        output = _Output(
            messages=[self._system_message, *base.messages[1:]],
            entry_ids=list(base.entry_ids),
        )
        for entry in batch:
            if isinstance(entry, MessageEntry):
                message, hidden = _visible_message(entry, edits)
                if not hidden:
                    output.emit(message, entry.id)
        return self._result(
            output,
            last_entry_id=batch[-1].id if batch else base.last_entry_id,
            reused_from=base.last_entry_id,
            edits=edits,
        )

    @staticmethod
    def _result(
        output: _Output,
        *,
        last_entry_id: str | None,
        reused_from: str | None,
        edits: dict[str, ContextEdit],
    ) -> BuildResult:
        return BuildResult(
            messages=output.messages,
            last_entry_id=last_entry_id,
            reused_from=reused_from,
            entry_ids=output.entry_ids,
            edits=edits,
        )
