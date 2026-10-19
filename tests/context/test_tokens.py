from __future__ import annotations

from vibe.core.context.tokens import (
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
    estimate_tool_tokens,
)
from vibe.core.types import (
    AvailableFunction,
    AvailableTool,
    FunctionCall,
    LLMMessage,
    LLMUsage,
    Role,
    ToolCall,
)


def make_message(role: Role, content: str) -> LLMMessage:
    return LLMMessage(role=role, content=content)


def make_tool(name: str, description: str) -> AvailableTool:
    return AvailableTool(
        function=AvailableFunction(
            name=name,
            description=description,
            parameters={"type": "object", "properties": {}},
        )
    )


def test_estimate_text_tokens_grows_with_length() -> None:
    assert estimate_text_tokens(None) == 0
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("short") < estimate_text_tokens("short" * 100)


def test_estimate_context_tokens_is_monotonic() -> None:
    messages = [make_message(Role.system, "system prompt")]
    base = estimate_context_tokens(messages)
    messages.append(make_message(Role.user, "hello"))
    with_user = estimate_context_tokens(messages)
    messages.append(make_message(Role.assistant, "hi there"))
    with_assistant = estimate_context_tokens(messages)

    assert base <= with_user <= with_assistant


def test_usage_anchor_uses_prompt_completion_and_tail() -> None:
    messages = [
        make_message(Role.system, "system prompt"),
        make_message(Role.user, "question"),
        make_message(Role.assistant, "answer"),
    ]
    usage = LLMUsage(prompt_tokens=1000, completion_tokens=50)
    tail = make_message(Role.user, "follow-up " * 50)
    messages.append(tail)

    estimate = estimate_context_tokens(
        messages, last_usage=usage, last_usage_message_index=2
    )

    assert estimate == 1000 + 50 + estimate_message_tokens(tail)


def test_usage_anchor_ignored_when_index_out_of_range() -> None:
    messages = [make_message(Role.user, "hello")]
    usage = LLMUsage(prompt_tokens=1000, completion_tokens=50)

    assert estimate_context_tokens(
        messages, last_usage=usage, last_usage_message_index=5
    ) == estimate_context_tokens(messages)


def test_tool_schemas_are_counted() -> None:
    tools = [
        make_tool("bash", "Run a shell command " * 10),
        make_tool("read_file", "Read a file " * 10),
    ]
    messages = [make_message(Role.user, "hello")]

    assert estimate_tool_tokens(tools) > 0
    assert estimate_context_tokens(messages, tools) > estimate_context_tokens(
        messages, []
    )


def test_assistant_tool_calls_counted() -> None:
    plain = make_message(Role.assistant, "text")
    with_tool_calls = LLMMessage(
        role=Role.assistant,
        content="text",
        tool_calls=[
            ToolCall(
                id="tc1",
                function=FunctionCall(name="bash", arguments='{"command": "ls"}'),
            )
        ],
    )

    assert estimate_message_tokens(with_tool_calls) > estimate_message_tokens(plain)
