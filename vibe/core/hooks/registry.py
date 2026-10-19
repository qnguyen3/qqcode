from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
from logging import getLogger
from typing import Any

from vibe.core.hooks.events import HookDecision, HookEvent

logger = getLogger("vibe")

DEFAULT_HOOK_TIMEOUT = 5.0

# Events whose decisions the agent acts on. Other events are fire-and-forget:
# handlers still run, but the dispatch result is always ``allow``.
DECISION_EVENT_TYPES = frozenset({
    "agent_start",
    "before_request",
    "tool_call",
    "before_compact",
})

type HookHandler = Callable[[Any], HookDecision | None | Awaitable[HookDecision | None]]


@dataclass(frozen=True)
class _RegisteredHandler:
    handler: HookHandler
    priority: int
    order: int


def _merge_decisions(base: HookDecision, new: HookDecision) -> HookDecision:
    """Merge ``new`` into ``base``.

    ``deny`` wins over ``modify``/``allow``; ``modify`` wins over ``allow``.
    Replacement args are merged key by key and injected messages appended, with
    later handlers winning for duplicate keys.
    """
    action = base.action
    match new.action:
        case "deny":
            action = "deny"
        case "modify" if action != "deny":
            action = "modify"

    reason = new.reason if new.reason is not None else base.reason

    replacement_args = base.replacement_args
    if new.replacement_args is not None:
        replacement_args = {**(base.replacement_args or {}), **new.replacement_args}

    return HookDecision(
        action=action,
        reason=reason,
        replacement_args=replacement_args,
        injected_messages=[*base.injected_messages, *new.injected_messages],
        summary_override=(
            new.summary_override
            if new.summary_override is not None
            else base.summary_override
        ),
    )


class HookRegistry:
    """Registers handlers per event type and dispatches events to them.

    Handlers may be sync or async. Each handler runs with a per-handler
    timeout; failures and timeouts are logged and isolated so a broken
    extension can never crash the agent.
    """

    def __init__(self, *, timeout: float = DEFAULT_HOOK_TIMEOUT) -> None:
        self.timeout = timeout
        self._handlers: dict[str, list[_RegisteredHandler]] = {}
        self._next_order = 0

    def register(
        self, event_type: str, handler: HookHandler, *, priority: int = 0
    ) -> None:
        """Register ``handler`` for ``event_type``; higher priority runs first."""
        self._handlers.setdefault(event_type, []).append(
            _RegisteredHandler(
                handler=handler, priority=priority, order=self._next_order
            )
        )
        self._next_order += 1

    def registered_types(self) -> list[str]:
        """Return the event types that currently have at least one handler."""
        return sorted(self._handlers)

    def has_handlers(self, event_type: str) -> bool:
        return bool(self._handlers.get(event_type))

    async def dispatch(self, event: HookEvent) -> HookDecision:
        """Run every handler for ``event.type`` and return a merged decision.

        Handlers run by descending priority, then registration order. An empty
        registry returns ``allow`` without any scheduling overhead.
        """
        handlers = self._handlers.get(event.type)
        if not handlers:
            return HookDecision()

        acts_on_decisions = event.type in DECISION_EVENT_TYPES
        decision = HookDecision()
        for entry in sorted(handlers, key=lambda item: item.priority, reverse=True):
            result = await self._invoke(entry.handler, event)
            if result is not None and acts_on_decisions:
                decision = _merge_decisions(decision, result)
        return decision

    async def _invoke(
        self, handler: HookHandler, event: HookEvent
    ) -> HookDecision | None:
        try:
            if inspect.iscoroutinefunction(handler):
                result: object = await asyncio.wait_for(
                    handler(event), timeout=self.timeout
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(handler, event), timeout=self.timeout
                )
        except TimeoutError:
            self._log_timeout(event)
            return None
        except Exception as exc:
            logger.warning("Hook handler for '%s' failed: %s", event.type, exc)
            return None

        if isinstance(result, HookDecision) or result is None:
            return result

        if asyncio.iscoroutine(result):
            try:
                result = await asyncio.wait_for(result, timeout=self.timeout)
            except TimeoutError:
                self._log_timeout(event)
                return None
            except Exception as exc:
                logger.warning("Hook handler for '%s' failed: %s", event.type, exc)
                return None

        if not isinstance(result, HookDecision):
            logger.warning(
                "Hook handler for '%s' returned unsupported value %r",
                event.type,
                result,
            )
        return result if isinstance(result, HookDecision) else None

    def _log_timeout(self, event: HookEvent) -> None:
        logger.warning(
            "Hook handler for '%s' timed out after %.2fs", event.type, self.timeout
        )
