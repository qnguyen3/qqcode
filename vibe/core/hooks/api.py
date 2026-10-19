from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from vibe.core.hooks.events import HookDecision, HookEvent
from vibe.core.hooks.registry import HookHandler
from vibe.core.types import LLMMessage, Role


@dataclass(frozen=True)
class HookBinding:
    """A handler bound to an event type, discovered by the hook loader."""

    event_type: str
    handler: HookHandler
    tool_name: str | None = None
    priority: int = 0

    def __call__(
        self, event: HookEvent
    ) -> HookDecision | None | Awaitable[HookDecision | None]:
        if (
            self.tool_name is not None
            and getattr(event, "tool_name", None) != self.tool_name
        ):
            return None
        return self.handler(event)


def on_event(
    event_type: str, *, priority: int = 0
) -> Callable[[HookHandler], HookBinding]:
    """Bind a handler to any lifecycle event type."""

    def decorator(handler: HookHandler) -> HookBinding:
        return HookBinding(event_type=event_type, handler=handler, priority=priority)

    return decorator


def on_tool_call(
    tool_name: str | None = None, *, priority: int = 0
) -> Callable[[HookHandler], HookBinding]:
    """Bind a handler to ``tool_call`` events, optionally for one tool name."""

    def decorator(handler: HookHandler) -> HookBinding:
        return HookBinding(
            event_type="tool_call",
            handler=handler,
            tool_name=tool_name,
            priority=priority,
        )

    return decorator


def deny(reason: str | None = None) -> HookDecision:
    """Block the current request or tool call."""
    return HookDecision(action="deny", reason=reason)


def modify(
    *,
    replacement_args: dict[str, Any] | None = None,
    injected_messages: list[LLMMessage] | None = None,
    summary_override: str | None = None,
    reason: str | None = None,
) -> HookDecision:
    """Replace tool call arguments, inject messages, or supply a summary."""
    return HookDecision(
        action="modify",
        reason=reason,
        replacement_args=replacement_args,
        injected_messages=list(injected_messages or []),
        summary_override=summary_override,
    )


def inject(
    message: str | LLMMessage, *, role: Role = Role.user, reason: str | None = None
) -> HookDecision:
    """Inject a message into the conversation for the current request."""
    injected = (
        message
        if isinstance(message, LLMMessage)
        else LLMMessage(role=role, content=message)
    )
    return modify(injected_messages=[injected], reason=reason)
