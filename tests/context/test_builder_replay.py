from __future__ import annotations

from vibe.core.context.builder import ContextBuilder
from vibe.core.sessions.entries import (
    SUMMARY_TAG,
    CompactionEntry,
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


def _assistant_tool_call(entry_id: str, call_id: str, name: str) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        timestamp="2026-01-01T00:00:00Z",
        message=LLMMessage(
            role=Role.assistant,
            content="",
            tool_calls=[
                ToolCall(id=call_id, function=FunctionCall(name=name, arguments="{}"))
            ],
        ),
    )


def test_head_marker_drops_previous_messages() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "first"),
        _msg("m2", Role.assistant, "reply"),
        HeadMarkerEntry(
            id="h1", timestamp="2026-01-01T00:00:01Z", keep_from_id=None, reason="clear"
        ),
        _msg("m3", Role.user, "after clear"),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "after clear"]
    assert result.entry_ids == [None, "m3"]
    assert result.last_entry_id == "m3"


def test_head_marker_keep_from_id_and_latest_marker_wins() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "one"),
        _msg("m2", Role.assistant, "two"),
        _msg("m3", Role.user, "three"),
        HeadMarkerEntry(
            id="h1", timestamp="2026-01-01T00:00:01Z", keep_from_id="m2", reason="reset"
        ),
        _msg("m4", Role.assistant, "four"),
        HeadMarkerEntry(
            id="h2", timestamp="2026-01-01T00:00:02Z", keep_from_id="m3", reason="reset"
        ),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.content for m in result.messages] == ["sys", "three", "four"]
    assert result.entry_ids == [None, "m3", "m4"]


def test_compaction_first_kept_id_wraps_summary_and_keeps_tail() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "old user"),
        _msg("m2", Role.assistant, "old assistant"),
        _msg("m3", Role.user, "kept user"),
        CompactionEntry(
            id="c1",
            timestamp="2026-01-01T00:00:01Z",
            first_kept_id="m3",
            summary="condensed history",
            tokens_before=100,
            tokens_after=20,
        ),
        _msg("m4", Role.assistant, "kept assistant"),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.role for m in result.messages] == [
        Role.system,
        Role.user,
        Role.user,
        Role.assistant,
    ]
    assert f"<{SUMMARY_TAG}>\ncondensed history\n</{SUMMARY_TAG}>" == (
        result.messages[1].content
    )
    assert [m.content for m in result.messages[2:]] == ["kept user", "kept assistant"]
    assert result.entry_ids == [None, "c1", "m3", "m4"]


def test_unanswered_tool_call_gets_placeholder_before_next_non_tool_message() -> None:
    entries: list[SessionEntry] = [
        _msg("m1", Role.user, "run grep"),
        _assistant_tool_call("m2", "call_1", "grep"),
        _msg("m3", Role.user, "follow up without tool result"),
    ]
    result = ContextBuilder(SYSTEM).build(entries)
    assert [m.role for m in result.messages] == [
        Role.system,
        Role.user,
        Role.assistant,
        Role.tool,
        Role.user,
    ]
    assert result.messages[3].tool_call_id == "call_1"
    assert result.messages[3].name == "grep"
    assert result.entry_ids == [None, "m1", "m2", None, "m3"]
