"""Inject a style reminder into every request sent to the model."""

from __future__ import annotations

from vibe.core.hooks.api import inject
from vibe.core.hooks.events import BeforeRequestEvent, HookDecision
from vibe.core.hooks.registry import HookRegistry

STYLE_REMINDER = (
    "Reminder: follow the project's existing style, keep changes minimal, "
    "and run the relevant tests before finishing."
)


def on_before_request(event: BeforeRequestEvent) -> HookDecision:
    return inject(STYLE_REMINDER)


def register(registry: HookRegistry) -> None:
    registry.register("before_request", on_before_request)
