from __future__ import annotations

from dataclasses import dataclass

from vibe.core.context.tokens import estimate_message_tokens
from vibe.core.types import LLMMessage, Role


@dataclass(frozen=True)
class CutResult:
    first_kept_index: int
    is_split_turn: bool


def _find_turn_start(messages: list[LLMMessage], index: int) -> int:
    for i in range(index, -1, -1):
        if messages[i].role is Role.user:
            return i
    return -1


def select_cut(messages: list[LLMMessage], keep_recent_tokens: int) -> CutResult:
    """Pick the first message of the recent window to keep verbatim.

    Walks backwards accumulating estimated tokens until ``keep_recent_tokens``
    is reached, then snaps to the closest valid cut point at or after the
    budget point. Only user and assistant messages are valid cut points, so a
    tool_call is never separated from its tool results.
    """
    if not messages:
        return CutResult(first_kept_index=0, is_split_turn=False)

    cut_points = [
        i
        for i, message in enumerate(messages)
        if message.role in {Role.user, Role.assistant}
    ]
    if not cut_points:
        return CutResult(first_kept_index=0, is_split_turn=False)

    accumulated = 0
    cut_index = cut_points[0]
    for i in range(len(messages) - 1, -1, -1):
        message_tokens = estimate_message_tokens(messages[i])
        if message_tokens == 0:
            continue
        accumulated += message_tokens
        if accumulated >= keep_recent_tokens:
            cut_index = next(
                (candidate for candidate in cut_points if candidate >= i),
                cut_points[-1],
            )
            break

    starts_turn = messages[cut_index].role is Role.user
    turn_start = _find_turn_start(messages, cut_index)
    is_split_turn = not starts_turn and turn_start != -1

    return CutResult(first_kept_index=cut_index, is_split_turn=is_split_turn)
