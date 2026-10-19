from __future__ import annotations

from math import ceil

from vibe.core.types import AvailableTool, LLMMessage, LLMUsage, Role

CHARS_PER_TOKEN = 3.5
IMAGE_CHARS = 4800
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_SCHEMA_OVERHEAD_TOKENS = 4


def estimate_text_tokens(text: str | None) -> int:
    if not text:
        return 0
    return ceil(len(text) / CHARS_PER_TOKEN)


def estimate_message_tokens(message: LLMMessage) -> int:
    total = MESSAGE_OVERHEAD_TOKENS
    total += estimate_text_tokens(message.content)
    total += estimate_text_tokens(message.reasoning_content)
    total += IMAGE_CHARS * len(message.images)
    if message.tool_calls:
        tool_calls_json = "".join(
            tool_call.model_dump_json(exclude_none=True)
            for tool_call in message.tool_calls
        )
        total += estimate_text_tokens(tool_calls_json)
    if message.role is Role.tool:
        total += estimate_text_tokens(message.name)
    return total


def estimate_tool_tokens(tools: list[AvailableTool]) -> int:
    return sum(
        TOOL_SCHEMA_OVERHEAD_TOKENS
        + estimate_text_tokens(tool.model_dump_json(exclude_none=True))
        for tool in tools
    )


def estimate_context_tokens(
    messages: list[LLMMessage],
    tools: list[AvailableTool] | None = None,
    last_usage: LLMUsage | None = None,
    last_usage_message_index: int | None = None,
) -> int:
    """Estimate context tokens, anchoring on the last known usage when possible.

    ``last_usage_message_index`` points at the message that closed the request
    which produced ``last_usage``. Everything after it is estimated locally, so
    the estimate stays ahead of the API call.
    """
    if (
        last_usage is not None
        and last_usage_message_index is not None
        and 0 <= last_usage_message_index < len(messages)
    ):
        trailing = sum(
            estimate_message_tokens(message)
            for message in messages[last_usage_message_index + 1 :]
        )
        return last_usage.prompt_tokens + last_usage.completion_tokens + trailing

    total = estimate_tool_tokens(tools or [])
    total += sum(estimate_message_tokens(message) for message in messages)
    return total
