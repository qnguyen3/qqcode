from __future__ import annotations

from vibe.core.context.cut import CutResult, select_cut
from vibe.core.context.tokens import estimate_message_tokens
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall


def user(content: str) -> LLMMessage:
    return LLMMessage(role=Role.user, content=content)


def assistant(content: str) -> LLMMessage:
    return LLMMessage(role=Role.assistant, content=content)


def assistant_with_tool(call_id: str, content: str = "") -> LLMMessage:
    return LLMMessage(
        role=Role.assistant,
        content=content,
        tool_calls=[
            ToolCall(
                id=call_id,
                function=FunctionCall(name="bash", arguments='{"command": "ls"}'),
            )
        ],
    )


def tool_result(call_id: str, content: str) -> LLMMessage:
    return LLMMessage(
        role=Role.tool, content=content, tool_call_id=call_id, name="bash"
    )


def assert_valid_cut(messages: list[LLMMessage], cut: CutResult) -> None:
    if cut.first_kept_index < len(messages):
        assert messages[cut.first_kept_index].role is not Role.tool

    for i in range(cut.first_kept_index):
        message = messages[i]
        if message.role is Role.assistant and message.tool_calls:
            call_ids = {tool_call.id for tool_call in message.tool_calls}
            result_ids = {
                result.tool_call_id
                for result in messages[i + 1 : cut.first_kept_index]
                if result.role is Role.tool
            }
            assert call_ids <= result_ids


def test_empty_history() -> None:
    assert select_cut([], keep_recent_tokens=100) == CutResult(0, False)


def test_single_message_history() -> None:
    assert select_cut([user("hello")], keep_recent_tokens=100) == CutResult(0, False)


def test_tool_result_is_never_first_kept_message() -> None:
    messages = [
        user("start"),
        assistant_with_tool("tc1"),
        tool_result("tc1", "X" * 5000),
        assistant("after tool"),
        user("next"),
        assistant("final"),
    ]

    cut = select_cut(messages, keep_recent_tokens=10)

    assert cut.first_kept_index == 4
    assert messages[cut.first_kept_index].role is Role.user
    assert cut.is_split_turn is False
    assert_valid_cut(messages, cut)


def test_recent_window_keeps_at_least_requested_tokens() -> None:
    messages = [
        user("a" * 400),
        assistant("b" * 400),
        user("c" * 400),
        assistant("d" * 400),
    ]

    cut = select_cut(messages, keep_recent_tokens=200)
    kept = sum(
        estimate_message_tokens(message) for message in messages[cut.first_kept_index :]
    )

    assert kept >= 200
    assert cut.first_kept_index == 2
    assert cut.is_split_turn is False


def test_cut_can_start_at_tool_calling_assistant() -> None:
    messages = [
        user("intro " * 10),
        assistant_with_tool("tc1", content="A" * 1000),
        tool_result("tc1", "ok"),
        assistant("done"),
    ]

    cut = select_cut(messages, keep_recent_tokens=100)

    assert cut.first_kept_index == 1
    assert messages[cut.first_kept_index].tool_calls is not None
    assert messages[cut.first_kept_index + 1].role is Role.tool
    assert_valid_cut(messages, cut)


def test_split_turn_when_starting_mid_turn() -> None:
    messages = [
        user("start"),
        assistant_with_tool("tc1"),
        tool_result("tc1", "Y" * 4000),
        assistant("follow up " * 50),
    ]

    cut = select_cut(messages, keep_recent_tokens=10)

    assert cut.is_split_turn is True
    assert_valid_cut(messages, cut)
