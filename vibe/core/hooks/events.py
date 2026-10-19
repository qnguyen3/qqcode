from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from vibe.core.types import LLMMessage, LLMUsage


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


class HookEvent(BaseModel):
    """Base class for lifecycle events dispatched to extension hooks.

    ``type`` is widened to ``Any`` so subclasses can narrow it to their event
    literal without tripping invariant-override checks.
    """

    type: Any
    timestamp: str = Field(default_factory=_utc_timestamp)


class AgentStartEvent(HookEvent):
    type: Literal["agent_start"] = "agent_start"
    session_id: str
    prompt: str


class BeforeRequestEvent(HookEvent):
    type: Literal["before_request"] = "before_request"
    messages: list[LLMMessage]
    model_alias: str


class AfterResponseEvent(HookEvent):
    type: Literal["after_response"] = "after_response"
    message: LLMMessage
    usage: LLMUsage


class ToolCallEventHook(HookEvent):
    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_call_id: str
    args: dict[str, Any]


class ToolResultEventHook(HookEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    tool_call_id: str
    ok: bool
    text: str | None = None
    error: str | None = None


class AgentEndEvent(HookEvent):
    type: Literal["agent_end"] = "agent_end"
    session_id: str
    usage: LLMUsage


class BeforeCompactEvent(HookEvent):
    type: Literal["before_compact"] = "before_compact"
    messages: list[LLMMessage]
    tokens_before: int
    reason: Literal["auto", "manual", "overflow"] = "auto"


class AfterCompactEvent(HookEvent):
    type: Literal["after_compact"] = "after_compact"
    tokens_before: int
    tokens_after: int
    is_update: bool


class CompactFailedEvent(HookEvent):
    type: Literal["compact_failed"] = "compact_failed"
    error: str


class HookDecision(BaseModel):
    """Decision returned by a hook handler for a lifecycle event."""

    action: Literal["allow", "deny", "modify"] = "allow"
    reason: str | None = None
    replacement_args: dict[str, Any] | None = None
    injected_messages: list[LLMMessage] = Field(default_factory=list)
    summary_override: str | None = None
