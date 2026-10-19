from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
import concurrent.futures
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum, StrEnum, auto
import functools
import logging
from pathlib import Path
import re
import sys
from typing import Any

import httpx

from vibe.core import __version__
from vibe.core.config import CONFIG_DIR, CONFIG_FILE, GLOBAL_CONFIG_FILE, Backend
from vibe.core.types import BaseEvent, ToolResultEvent


class ApprovalResponse(StrEnum):
    YES = "y"
    NO = "n"
    ALWAYS = "a"


CANCELLATION_TAG = "user_cancellation"
TOOL_ERROR_TAG = "tool_error"
VIBE_STOP_EVENT_TAG = "vibe_stop_event"
VIBE_WARNING_TAG = "vibe_warning"

KNOWN_TAGS = [CANCELLATION_TAG, TOOL_ERROR_TAG, VIBE_STOP_EVENT_TAG, VIBE_WARNING_TAG]


class TaggedText:
    _TAG_PATTERN = re.compile(
        rf"<({'|'.join(re.escape(tag) for tag in KNOWN_TAGS)})>(.*?)</\1>",
        flags=re.DOTALL,
    )

    def __init__(self, message: str, tag: str = "") -> None:
        self.message = message
        self.tag = tag

    def __str__(self) -> str:
        if not self.tag:
            return self.message
        return f"<{self.tag}>{self.message}</{self.tag}>"

    @staticmethod
    def from_string(text: str) -> TaggedText:
        found_tag = ""
        result = text

        def replace_tag(match: re.Match[str]) -> str:
            nonlocal found_tag
            tag_name = match.group(1)
            content = match.group(2)
            if not found_tag:
                found_tag = tag_name
            return content

        result = TaggedText._TAG_PATTERN.sub(replace_tag, text)

        if found_tag:
            return TaggedText(result, found_tag)

        return TaggedText(text, "")


class CancellationReason(Enum):
    OPERATION_CANCELLED = auto()
    TOOL_INTERRUPTED = auto()
    TOOL_NO_RESPONSE = auto()
    TOOL_SKIPPED = auto()


def get_user_cancellation_message(
    cancellation_reason: CancellationReason, tool_name: str | None = None
) -> TaggedText:
    match cancellation_reason:
        case CancellationReason.OPERATION_CANCELLED:
            return TaggedText("User cancelled the operation.", CANCELLATION_TAG)
        case CancellationReason.TOOL_INTERRUPTED:
            return TaggedText("Tool execution interrupted by user.", CANCELLATION_TAG)
        case CancellationReason.TOOL_NO_RESPONSE:
            return TaggedText(
                "Tool execution interrupted - no response available", CANCELLATION_TAG
            )
        case CancellationReason.TOOL_SKIPPED:
            return TaggedText(
                tool_name or "Tool execution skipped by user.", CANCELLATION_TAG
            )


def is_user_cancellation_event(event: BaseEvent) -> bool:
    return (
        isinstance(event, ToolResultEvent)
        and event.skipped
        and event.skip_reason is not None
        and f"<{CANCELLATION_TAG}>" in event.skip_reason
    )


def is_dangerous_directory(path: Path | str = ".") -> tuple[bool, str]:
    """Check if the current directory is a dangerous folder that would cause
    issues if we were to run the tool there.

    Args:
        path: Path to check (defaults to current directory)

    Returns:
        tuple[bool, str]: (is_dangerous, reason) where reason explains why it's dangerous
    """
    path = Path(path).resolve()

    home_dir = Path.home()

    dangerous_paths = {
        home_dir: "home directory",
        home_dir / "Documents": "Documents folder",
        home_dir / "Desktop": "Desktop folder",
        home_dir / "Downloads": "Downloads folder",
        home_dir / "Pictures": "Pictures folder",
        home_dir / "Movies": "Movies folder",
        home_dir / "Music": "Music folder",
        home_dir / "Library": "Library folder",
        Path("/Applications"): "Applications folder",
        Path("/System"): "System folder",
        Path("/Library"): "System Library folder",
        Path("/usr"): "System usr folder",
        Path("/private"): "System private folder",
    }

    for dangerous_path, description in dangerous_paths.items():
        try:
            if path == dangerous_path:
                return True, f"You are in the {description}"
        except (OSError, ValueError):
            continue
    return False, ""


LOG_DIR = CONFIG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "vibe.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, "a", "utf-8")],
)
logger = logging.getLogger("vibe")
logger.info("Using config: %s", CONFIG_FILE)
if CONFIG_FILE != GLOBAL_CONFIG_FILE and GLOBAL_CONFIG_FILE.is_file():
    logger.warning(
        "Project config active (%s); ignoring global config (%s)",
        CONFIG_FILE,
        GLOBAL_CONFIG_FILE,
    )


def get_user_agent(backend: Backend) -> str:
    user_agent = f"QQcode/{__version__}"
    if backend == Backend.MISTRAL:
        mistral_sdk_prefix = "mistral-client-python/"
        user_agent = f"{mistral_sdk_prefix}{user_agent}"
    return user_agent


def _is_retryable_http_error(e: Exception) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    return False


MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_delay(
    attempt: int, delay_seconds: float, backoff_factor: float, retry_after: float | None
) -> float:
    current_delay = (delay_seconds * (backoff_factor**attempt)) + (0.05 * attempt)
    if retry_after is None:
        return current_delay
    return max(current_delay, retry_after)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Parse a ``Retry-After`` header as seconds or an HTTP date.

    Returns:
        The requested delay in seconds, or None when the header is absent or
        malformed.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("retry-after")
    if raw is None or not (value := raw.strip()):
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def async_retry[T, **P](
    tries: int = 3,
    delay_seconds: float = 0.5,
    backoff_factor: float = 2.0,
    is_retryable: Callable[[Exception], bool] = _is_retryable_http_error,
    get_retry_after: Callable[[Exception], float | None] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Args:
        tries: Number of retry attempts
        delay_seconds: Initial delay between retries in seconds
        backoff_factor: Multiplier for delay on each retry
        is_retryable: Function to determine if an exception should trigger a retry
                     (defaults to checking for retryable HTTP errors from both urllib and httpx)
        get_retry_after: Optional parser for a server-requested retry delay. When
                     it returns a delay larger than the 60s cap, retrying stops.

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exc = None
            for attempt in range(tries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < tries - 1 and is_retryable(e):
                        retry_after = get_retry_after(e) if get_retry_after else None
                        if (
                            retry_after is not None
                            and retry_after > MAX_RETRY_AFTER_SECONDS
                        ):
                            raise
                        current_delay = _retry_delay(
                            attempt, delay_seconds, backoff_factor, retry_after
                        )
                        await asyncio.sleep(current_delay)
                        continue
                    raise e
            raise RuntimeError(
                f"Retries exhausted. Last error: {last_exc}"
            ) from last_exc

        return wrapper

    return decorator


def async_generator_retry[T, **P](
    tries: int = 3,
    delay_seconds: float = 0.5,
    backoff_factor: float = 2.0,
    is_retryable: Callable[[Exception], bool] = _is_retryable_http_error,
    get_retry_after: Callable[[Exception], float | None] | None = None,
) -> Callable[[Callable[P, AsyncGenerator[T]]], Callable[P, AsyncGenerator[T]]]:
    """Retry decorator for async generators.

    Args:
        tries: Number of retry attempts
        delay_seconds: Initial delay between retries in seconds
        backoff_factor: Multiplier for delay on each retry
        is_retryable: Function to determine if an exception should trigger a retry
                     (defaults to checking for retryable HTTP errors from both urllib and httpx)
        get_retry_after: Optional parser for a server-requested retry delay. When
                     it returns a delay larger than the 60s cap, retrying stops.

    Returns:
        Decorated async generator function with retry logic
    """

    def decorator(
        func: Callable[P, AsyncGenerator[T]],
    ) -> Callable[P, AsyncGenerator[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[T]:
            last_exc = None
            for attempt in range(tries):
                try:
                    async for item in func(*args, **kwargs):
                        yield item
                    return
                except Exception as e:
                    last_exc = e
                    if attempt < tries - 1 and is_retryable(e):
                        retry_after = get_retry_after(e) if get_retry_after else None
                        if (
                            retry_after is not None
                            and retry_after > MAX_RETRY_AFTER_SECONDS
                        ):
                            raise
                        current_delay = _retry_delay(
                            attempt, delay_seconds, backoff_factor, retry_after
                        )
                        await asyncio.sleep(current_delay)
                        continue
                    raise e
            raise RuntimeError(
                f"Retries exhausted. Last error: {last_exc}"
            ) from last_exc

        return wrapper

    return decorator


class ConversationLimitException(Exception):
    pass


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine synchronously, handling nested event loops.

    If called from within an async context (running event loop), runs the
    coroutine in a thread pool executor. Otherwise, uses asyncio.run().

    This mirrors the pattern used by ToolManager for MCP integration.
    """
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    except RuntimeError:
        return asyncio.run(coro)


def is_windows() -> bool:
    return sys.platform == "win32"
