from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

OVERFLOW_PATTERNS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "input is too long",
    "reduce the length",
    "context_length_exceeded",
    "max_tokens",
    "token limit",
)

OVERFLOW_STATUS_CODES = frozenset({400, 413, 422})


def _iter_error_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _error_status(error: BaseException) -> int | None:
    status: Any = getattr(error, "status", None)
    if not isinstance(status, int):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _error_text(error: BaseException) -> str:
    parts = [str(error)]
    for attribute in ("parsed_error", "body_text", "reason", "detail"):
        value = getattr(error, attribute, None)
        if isinstance(value, str):
            parts.append(value)
    if isinstance(error, httpx.HTTPStatusError):
        try:
            parts.append(error.response.text)
        except httpx.ResponseNotRead:
            # Streaming responses must be read before their body is available.
            pass
    return " ".join(parts).lower()


def _matches_pattern(error: BaseException) -> bool:
    text = _error_text(error)
    return any(pattern in text for pattern in OVERFLOW_PATTERNS)


def is_context_overflow(error: BaseException) -> bool:
    """Detect provider context-window overflow errors.

    Checks the whole exception chain for HTTP 400/413/422 responses whose
    message matches a known overflow pattern. Plain ``RuntimeError`` messages
    are matched on text alone, since the agent wraps backend errors without a
    status attribute.
    """
    for candidate in _iter_error_chain(error):
        if _error_status(candidate) in OVERFLOW_STATUS_CODES:
            if _matches_pattern(candidate):
                return True
            continue
        if isinstance(candidate, httpx.HTTPStatusError):
            continue
        if type(candidate) is RuntimeError and _matches_pattern(candidate):
            return True
    return False
