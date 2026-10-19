"""Shared versioned event bus and event models for the CLI and Embedding SDK."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, final
import uuid

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    CompactEndEvent,
    StreamEventType,
    ToolCallEvent,
    ToolResultEvent,
)

EVENT_SCHEMA_VERSION: Literal[1] = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SDKEventBase(BaseModel):
    """Common envelope fields shared by all versioned stream events."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(
        default=EVENT_SCHEMA_VERSION, alias="schema", serialization_alias="schema"
    )
    timestamp: str = Field(default_factory=_now_iso)

    def to_wire_dict(self) -> dict[str, Any]:
        """Serialize with the wire key ``schema`` and field order ``type, schema, timestamp``."""
        payload = self.model_dump(mode="json", by_alias=True)
        ordered: dict[str, Any] = {
            "type": payload.pop("type"),
            "schema": payload.pop("schema", EVENT_SCHEMA_VERSION),
            "timestamp": payload.pop("timestamp"),
        }
        ordered.update(payload)
        return ordered


@final
class SessionStartedEvent(SDKEventBase):
    type: Literal["session.started"] = "session.started"
    session_id: str


@final
class TurnStartedEvent(SDKEventBase):
    type: Literal["turn.started"] = "turn.started"
    turn: int


@final
class MessageDeltaEvent(SDKEventBase):
    type: Literal["message.delta"] = "message.delta"
    item_id: str
    delta: str
    content: str


@final
class ThinkingUpdatedEvent(SDKEventBase):
    type: Literal["thinking.updated"] = "thinking.updated"
    item_id: str
    delta: str
    content: str


@final
class ToolCallStreamEvent(SDKEventBase):
    type: Literal["tool.call"] = "tool.call"
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


@final
class ToolResultStreamEvent(SDKEventBase):
    type: Literal["tool.result"] = "tool.result"
    tool_call_id: str
    tool_name: str
    status: Literal["ok", "error", "skipped"]
    text: str
    truncated: bool = False


class TurnUsage(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@final
class TurnCompletedEvent(SDKEventBase):
    type: Literal["turn.completed"] = "turn.completed"
    usage: TurnUsage = Field(default_factory=TurnUsage)
    cost_usd: float = 0.0


@final
class SessionCompactedEvent(SDKEventBase):
    type: Literal["session.compacted"] = "session.compacted"
    tokens_before: int
    tokens_after: int


@final
class ToolApprovalRequiredEvent(SDKEventBase):
    type: Literal["tool.approval_required"] = "tool.approval_required"
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


@final
class PlanApprovalRequiredEvent(SDKEventBase):
    type: Literal["plan.approval_required"] = "plan.approval_required"
    plan: str


@final
class ErrorStreamEvent(SDKEventBase):
    type: Literal["error"] = "error"
    message: str


SDKEvent = Annotated[
    SessionStartedEvent
    | TurnStartedEvent
    | MessageDeltaEvent
    | ThinkingUpdatedEvent
    | ToolCallStreamEvent
    | ToolResultStreamEvent
    | TurnCompletedEvent
    | SessionCompactedEvent
    | ToolApprovalRequiredEvent
    | PlanApprovalRequiredEvent
    | ErrorStreamEvent,
    Field(discriminator="type"),
]

SDK_EVENT_ADAPTER: TypeAdapter[SDKEvent] = TypeAdapter(SDKEvent)

SDK_EVENT_TYPES: tuple[str, ...] = (
    "session.started",
    "turn.started",
    "message.delta",
    "thinking.updated",
    "tool.call",
    "tool.result",
    "turn.completed",
    "session.compacted",
    "tool.approval_required",
    "plan.approval_required",
    "error",
)


def parse_sdk_event(data: bytes | str | dict[str, Any]) -> SDKEvent:
    """Validate a JSON string/bytes or dict into the discriminated ``SDKEvent`` union."""
    if isinstance(data, bytes | str):
        return SDK_EVENT_ADAPTER.validate_json(data)
    return SDK_EVENT_ADAPTER.validate_python(data)


def sdk_event_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for the discriminated ``SDKEvent`` union."""
    return SDK_EVENT_ADAPTER.json_schema()


type EventSubscriber = Callable[[SDKEvent], None]


class EventBus:
    """Synchronous pub/sub bus with async queue subscription support."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        """Register ``callback`` and return an unsubscribe function."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    def subscribe_queue(
        self,
    ) -> tuple[asyncio.Queue[SDKEvent | None], Callable[[], None]]:
        """Subscribe an ``asyncio.Queue`` that receives every published ``SDKEvent``."""
        queue: asyncio.Queue[SDKEvent | None] = asyncio.Queue()
        unsubscribe = self.subscribe(queue.put_nowait)
        return queue, unsubscribe

    def publish(self, event: SDKEvent) -> None:
        for subscriber in list(self._subscribers):
            subscriber(event)


class AgentEventTranslator:
    """Stateful translator from internal ``Agent`` events to ``SDKEvent`` records."""

    def __init__(self, bus: EventBus, session_id: str) -> None:
        self.bus = bus
        self.session_id = session_id
        self._turn = 0
        self._item_id: str | None = None
        self._content = ""
        self._reasoning = ""
        self._usage = TurnUsage()
        self._cost_usd = 0.0

    def start_session(self) -> None:
        self.bus.publish(SessionStartedEvent(session_id=self.session_id))

    def emit_error(self, message: str) -> None:
        self.bus.publish(ErrorStreamEvent(message=message))

    def emit_raw(self, event_type: StreamEventType, data: dict[str, Any]) -> None:
        payload = {"type": str(event_type), **data}
        self.bus.publish(parse_sdk_event(payload))

    def on_event(self, event: BaseEvent) -> None:
        match event:
            case AssistantEvent():
                self._handle_assistant_event(event)
            case ToolCallEvent():
                self.bus.publish(
                    ToolCallStreamEvent(
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        args=event.args.model_dump(mode="json"),
                    )
                )
            case ToolResultEvent():
                self._emit_tool_result(event)
            case CompactEndEvent():
                self.bus.publish(
                    SessionCompactedEvent(
                        tokens_before=event.old_context_tokens,
                        tokens_after=event.new_context_tokens,
                    )
                )

    def _emit_tool_result(self, event: ToolResultEvent) -> None:
        match event:
            case ToolResultEvent(error=error) if error is not None:
                status: Literal["ok", "error", "skipped"] = "error"
                text = error
            case ToolResultEvent(skipped=True, skip_reason=reason):
                status = "skipped"
                text = reason or ""
            case ToolResultEvent(result=result) if result is not None:
                status = "ok"
                text = result.model_dump_json()
            case _:
                status = "ok"
                text = ""

        self.bus.publish(
            ToolResultStreamEvent(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                status=status,
                text=text,
                truncated=False,
            )
        )

    def _handle_assistant_event(self, event: AssistantEvent) -> None:
        if self._item_id is None:
            self._turn += 1
            self._item_id = f"msg_{uuid.uuid4().hex[:8]}"
            self._content = ""
            self.bus.publish(TurnStartedEvent(turn=self._turn))

        if event.reasoning_content:
            reasoning_delta = event.reasoning_content[len(self._reasoning) :]
            if reasoning_delta:
                self._reasoning = event.reasoning_content
                self.bus.publish(
                    ThinkingUpdatedEvent(
                        item_id=self._item_id,
                        delta=reasoning_delta,
                        content=self._reasoning,
                    )
                )

        content_delta = event.content[len(self._content) :] if event.content else ""
        if content_delta:
            self._content = event.content
            self.bus.publish(
                MessageDeltaEvent(
                    item_id=self._item_id, delta=content_delta, content=self._content
                )
            )

        if event.cache_read_tokens:
            self._usage.cache_read = event.cache_read_tokens
        if event.cache_write_tokens:
            self._usage.cache_write = event.cache_write_tokens
        if event.prompt_tokens:
            self._usage.input = event.prompt_tokens
        if event.completion_tokens:
            self._usage.output = event.completion_tokens
        if event.cost_usd:
            self._cost_usd = event.cost_usd

        if event.stopped_by_middleware or event.completion_tokens > 0:
            self.bus.publish(
                TurnCompletedEvent(
                    usage=self._usage.model_copy(), cost_usd=round(self._cost_usd, 6)
                )
            )
            self._item_id = None
            self._content = ""
            self._reasoning = ""
            self._usage = TurnUsage()
            self._cost_usd = 0.0
