from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum, auto

MINIMUM_INTERVAL_SECONDS = 1.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
SLOW_DOWN_INCREMENT_SECONDS = 5.0

CANCEL_MESSAGE = "Login cancelled"
TIMEOUT_MESSAGE = "Device flow timed out"
SLOW_DOWN_TIMEOUT_MESSAGE = (
    "Device flow timed out after one or more slow_down responses. This is often "
    "caused by clock drift in WSL or VM environments. Please sync or restart the "
    "VM clock and try again."
)


class DeviceFlowError(RuntimeError):
    """Base error for device-code polling."""


class DeviceFlowCancelled(DeviceFlowError):
    """Login was cancelled by the user."""


class DeviceFlowTimeout(DeviceFlowError):
    """The device flow deadline elapsed."""


class PollStatus(StrEnum):
    """Outcome of a single device-code poll attempt."""

    PENDING = auto()
    SLOW_DOWN = auto()
    FAILED = auto()
    COMPLETE = auto()


@dataclass(frozen=True)
class PollResult[T]:
    """Result of one poll, telling the poller whether to continue."""

    status: PollStatus
    value: T | None = None
    message: str | None = None
    interval_seconds: float | None = None

    @classmethod
    def pending(cls) -> PollResult[T]:
        """The user has not approved yet; keep polling."""
        return cls(PollStatus.PENDING)

    @classmethod
    def slow_down(cls, interval_seconds: float | None = None) -> PollResult[T]:
        """The server asked for a slower poll interval."""
        return cls(PollStatus.SLOW_DOWN, interval_seconds=interval_seconds)

    @classmethod
    def failed(cls, message: str) -> PollResult[T]:
        """Polling cannot continue."""
        return cls(PollStatus.FAILED, message=message)

    @classmethod
    def complete(cls, value: T) -> PollResult[T]:
        """Authorization finished with a value."""
        return cls(PollStatus.COMPLETE, value=value)


async def abortable_sleep(seconds: float, cancel_event: asyncio.Event) -> None:
    """Sleep, raising :class:`DeviceFlowCancelled` when cancellation is requested."""
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
    except TimeoutError:
        return
    raise DeviceFlowCancelled(CANCEL_MESSAGE)


def _next_interval(current: float, server_interval: float | None) -> float:
    """Apply RFC 8628 slow_down: trust a server interval, else add five seconds."""
    if server_interval is not None and server_interval > 0:
        return max(MINIMUM_INTERVAL_SECONDS, server_interval)
    return max(MINIMUM_INTERVAL_SECONDS, current + SLOW_DOWN_INCREMENT_SECONDS)


async def _sleep_within_deadline(
    interval: float, deadline: float | None, cancel_event: asyncio.Event
) -> None:
    if deadline is not None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        interval = min(interval, remaining)
    await abortable_sleep(interval, cancel_event)


async def poll_device_code_flow[T](
    *,
    poll: Callable[[], Awaitable[PollResult[T]]],
    cancel_event: asyncio.Event,
    interval_seconds: float | None = None,
    expires_in_seconds: float | None = None,
    wait_before_first_poll: bool = False,
) -> T:
    """Poll a device-code token endpoint until it completes, times out, or cancels.

    Raises:
        DeviceFlowCancelled: When the user aborts.
        DeviceFlowTimeout: When ``expires_in_seconds`` elapses first.
        DeviceFlowError: When a poll reports a non-recoverable failure.
    """
    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + expires_in_seconds if expires_in_seconds is not None else None
    )
    interval = max(
        MINIMUM_INTERVAL_SECONDS,
        interval_seconds
        if interval_seconds and interval_seconds > 0
        else DEFAULT_POLL_INTERVAL_SECONDS,
    )
    slow_downs = 0

    if wait_before_first_poll:
        await _sleep_within_deadline(interval, deadline, cancel_event)

    while deadline is None or loop.time() < deadline:
        if cancel_event.is_set():
            raise DeviceFlowCancelled(CANCEL_MESSAGE)

        result = await poll()
        match result.status:
            case PollStatus.COMPLETE:
                if result.value is None:
                    raise DeviceFlowError("Device flow completed without a value")
                return result.value
            case PollStatus.FAILED:
                raise DeviceFlowError(result.message or "Device flow failed")
            case PollStatus.SLOW_DOWN:
                slow_downs += 1
                interval = _next_interval(interval, result.interval_seconds)
            case PollStatus.PENDING:
                pass

        await _sleep_within_deadline(interval, deadline, cancel_event)

    raise DeviceFlowTimeout(
        SLOW_DOWN_TIMEOUT_MESSAGE if slow_downs else TIMEOUT_MESSAGE
    )
