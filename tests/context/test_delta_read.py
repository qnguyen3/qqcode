from __future__ import annotations

from vibe.core.context import builder as builder_module
from vibe.core.context.builder import BuildResult, ContextBuilder
from vibe.core.sessions.entries import (
    CompactionEntry,
    ContextEditEntry,
    HeadMarkerEntry,
    MessageEntry,
    SessionEntry,
)
from vibe.core.types import LLMMessage, Role

SYSTEM = LLMMessage(role=Role.system, content="sys")


def _msg(index: int) -> MessageEntry:
    role = Role.user if index % 2 == 0 else Role.assistant
    return MessageEntry(
        id=f"m{index:04d}",
        timestamp="2026-01-01T00:00:00Z",
        message=LLMMessage(role=role, content=f"msg-{index}"),
    )


def test_delta_read_200_entries_replays_only_new_entries(monkeypatch) -> None:
    builder = ContextBuilder(SYSTEM)
    entries: list[SessionEntry] = [_msg(0)]
    base: BuildResult | None = builder.build(entries)

    calls: list[int] = []
    original_visible = builder_module._visible_message

    def counting_visible(entry: MessageEntry, edits):
        calls.append(1)
        return original_visible(entry, edits)

    monkeypatch.setattr(builder_module, "_visible_message", counting_visible)

    for idx in range(1, 200):
        calls.clear()
        entries.append(_msg(idx))
        base = builder.build_from(entries, base)
        assert len(calls) == 1
        assert base.reused_from == f"m{idx - 1:04d}"
        assert base.last_entry_id == f"m{idx:04d}"

    full = builder.build(entries)
    assert base is not None
    assert base.messages == full.messages
    assert base.entry_ids == full.entry_ids


def test_delta_invalidates_on_head_marker_compaction_or_edit_into_base() -> None:
    builder = ContextBuilder(SYSTEM)
    entries: list[SessionEntry] = [_msg(0), _msg(1), _msg(2)]
    base = builder.build(entries)

    with_edit = [
        *entries,
        ContextEditEntry(
            id="e1", timestamp="2026-01-01T00:00:01Z", target_id="m0001", action="omit"
        ),
    ]
    rebuilt_edit = builder.build_from(with_edit, base)
    assert rebuilt_edit.reused_from is None
    assert rebuilt_edit.messages == builder.build(with_edit).messages

    with_marker = [
        *entries,
        HeadMarkerEntry(
            id="h1",
            timestamp="2026-01-01T00:00:01Z",
            keep_from_id="m0002",
            reason="reset",
        ),
    ]
    rebuilt_marker = builder.build_from(with_marker, base)
    assert rebuilt_marker.reused_from is None
    assert rebuilt_marker.messages == builder.build(with_marker).messages

    with_compaction = [
        *entries,
        CompactionEntry(
            id="c1",
            timestamp="2026-01-01T00:00:01Z",
            first_kept_id="m0002",
            summary="short summary",
            tokens_before=50,
            tokens_after=10,
        ),
    ]
    rebuilt_compaction = builder.build_from(with_compaction, base)
    assert rebuilt_compaction.reused_from is None
    assert rebuilt_compaction.messages == builder.build(with_compaction).messages
