from __future__ import annotations

from vibe.core.context.builder import ContextBuilder
from vibe.core.sessions.entries import (
    ContextEditEntry,
    HeadMarkerEntry,
    MessageEntry,
    SessionEntry,
)
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall

SYSTEM = LLMMessage(role=Role.system, content="sys")


def _msg(entry_id: str, role: Role, content: str) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        timestamp="2026-01-01T00:00:00Z",
        message=LLMMessage(role=role, content=content),
    )


def test_omit_removes_single_message_from_context() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "keep me"),
        _msg("m2", Role.assistant, "drop me"),
        _msg("m3", Role.user, "also keep"),
        ContextEditEntry(
            id="e1", timestamp="2026-01-01T00:00:01Z", target_id="m2", action="omit"
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "keep me", "also keep"]
    assert result.entry_ids == [None, "m1", "m3"]


def test_replace_rewrites_message_content_without_mutating_entry() -> None:
    original = _msg("m1", Role.user, "secret token 123")
    entries: list[SessionEntry] = [
        original,
        ContextEditEntry(
            id="e1",
            timestamp="2026-01-01T00:00:01Z",
            target_id="m1",
            action="replace",
            replacement="[redacted]",
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "[redacted]"]
    assert original.message.content == "secret token 123"


def test_later_edit_overrides_earlier_edit_on_same_target() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "initial"),
        ContextEditEntry(
            id="e1", timestamp="2026-01-01T00:00:01Z", target_id="m1", action="omit"
        ),
        ContextEditEntry(
            id="e2",
            timestamp="2026-01-01T00:00:02Z",
            target_id="m1",
            action="replace",
            replacement="restored and edited",
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "restored and edited"]


def test_edit_targeting_entry_before_head_marker_is_ignored() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "before marker"),
        HeadMarkerEntry(
            id="h1", timestamp="2026-01-01T00:00:01Z", keep_from_id=None, reason="clear"
        ),
        _msg("m2", Role.user, "after marker"),
        ContextEditEntry(
            id="e1",
            timestamp="2026-01-01T00:00:02Z",
            target_id="m1",
            action="replace",
            replacement="should not reappear",
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "after marker"]


def test_omitting_tool_response_inserts_placeholder_so_call_is_not_orphaned() -> None:
    entries: list[SessionEntry] = [
        MessageEntry(
            id="m1",
            timestamp="2026-01-01T00:00:00Z",
            message=LLMMessage(
                role=Role.assistant,
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc_1",
                        function=FunctionCall(name="read_file", arguments="{}"),
                    )
                ],
            ),
        ),
        MessageEntry(
            id="m2",
            timestamp="2026-01-01T00:00:01Z",
            message=LLMMessage(
                role=Role.tool,
                tool_call_id="tc_1",
                name="read_file",
                content="huge file output",
            ),
        ),
        _msg("m3", Role.assistant, "done"),
        ContextEditEntry(
            id="e1", timestamp="2026-01-01T00:00:02Z", target_id="m2", action="omit"
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.role for m in result.messages] == [
        Role.system,
        Role.assistant,
        Role.tool,
        Role.assistant,
    ]
    assert result.messages[2].tool_call_id == "tc_1"
    assert result.messages[2].content != "huge file output"
