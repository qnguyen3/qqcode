from __future__ import annotations

from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from vibe.core.config import ModelPricing
from vibe.core.tools.base import BaseTool


class AgentMode(StrEnum):
    """Operating mode for the agent."""

    INTERACTIVE = "interactive"  # Default - asks for approval on tool calls
    AUTO_APPROVE = "auto_approve"  # Executes all tools without asking
    PLAN = "plan"  # Read-only operations only, for planning


@dataclass
class ResumeSessionInfo:
    type: Literal["continue", "resume"]
    session_id: str
    session_time: str

    def message(self) -> str:
        action = None
        match self.type:
            case "continue":
                action = "Continuing"
            case "resume":
                action = "Resuming"
        return f"{action} session `{self.session_id}` from {self.session_time}"


class AgentStats(BaseModel):
    steps: int = 0
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls_agreed: int = 0
    tool_calls_rejected: int = 0
    tool_calls_failed: int = 0
    tool_calls_succeeded: int = 0

    context_tokens: int = 0
    context_window: int = 0
    last_usage_message_index: int = -1

    last_turn_prompt_tokens: int = 0
    last_turn_completion_tokens: int = 0
    last_turn_duration: float = 0.0
    tokens_per_second: float = 0.0

    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    cache_read_price_per_million: float = 0.0
    cache_write_price_per_million: float = 0.0

    @computed_field
    @property
    def context_percent(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return min(100.0, (self.context_tokens / self.context_window) * 100.0)

    @computed_field
    @property
    def session_total_llm_tokens(self) -> int:
        return self.session_prompt_tokens + self.session_completion_tokens

    @computed_field
    @property
    def last_turn_total_tokens(self) -> int:
        return self.last_turn_prompt_tokens + self.last_turn_completion_tokens

    def usage_cost(self, usage: LLMUsage) -> float:
        """Cost in dollars for one usage record, including cache read/write."""
        return self._cost(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    def _cost(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> float:
        billed_input = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)
        total = (
            billed_input * self.input_price_per_million
            + completion_tokens * self.output_price_per_million
            + cache_read_tokens * self.cache_read_price_per_million
            + cache_write_tokens * self.cache_write_price_per_million
        )
        return round(total / 1_000_000, 6)

    @computed_field
    @property
    def session_cost(self) -> float:
        """Calculate the total session cost in dollars based on token usage and pricing.

        Cache read/write tokens are billed at their own price and excluded from
        the regular input price. If the model changes mid-session, this uses
        current pricing for all tokens.
        """
        return self._cost(
            prompt_tokens=self.session_prompt_tokens,
            completion_tokens=self.session_completion_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )

    @computed_field
    @property
    def session_cache_cost(self) -> float:
        """Portion of :attr:`session_cost` spent on cache reads/writes."""
        total = (
            self.cache_read_tokens * self.cache_read_price_per_million
            + self.cache_write_tokens * self.cache_write_price_per_million
        )
        return round(total / 1_000_000, 6)

    def update_pricing(self, pricing: ModelPricing) -> None:
        """Update pricing info when model changes.

        NOTE: session_cost will be recalculated using new pricing for all
        accumulated tokens. This is a known approximation when models change.
        This should not be a big issue, pricing is only used for max_price which is in
        programmatic mode, so user should not update models there.
        """
        self.input_price_per_million = pricing.input_per_million
        self.output_price_per_million = pricing.output_per_million
        self.cache_read_price_per_million = pricing.cache_read_per_million
        self.cache_write_price_per_million = pricing.cache_write_per_million

    def reset_context_state(self) -> None:
        """Reset context-related fields while preserving cumulative session stats.

        Used after config reload or similar operations where the context
        changes but we want to preserve session totals.
        """
        self.context_tokens = 0
        self.last_usage_message_index = -1
        self.last_turn_prompt_tokens = 0
        self.last_turn_completion_tokens = 0
        self.last_turn_duration = 0.0
        self.tokens_per_second = 0.0


class SessionInfo(BaseModel):
    session_id: str
    start_time: str
    message_count: int
    stats: AgentStats
    save_dir: str


class SessionMetadata(BaseModel):
    session_id: str
    start_time: str
    end_time: str | None
    git_commit: str | None
    git_branch: str | None
    environment: dict[str, str | None]
    auto_approve: bool = False
    username: str


StrToolChoice = Literal["auto", "none", "any", "required"]


class AvailableFunction(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class AvailableTool(BaseModel):
    type: Literal["function"] = "function"
    function: AvailableFunction


class FunctionCall(BaseModel):
    name: str | None = None
    arguments: str | None = None


class ToolCall(BaseModel):
    id: str | None = None
    index: int | None = None
    function: FunctionCall = Field(default_factory=FunctionCall)
    type: str = "function"


def _content_before(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts: list[str] = []
        for p in v:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(v)


Content = Annotated[str, BeforeValidator(_content_before)]


class ImagePart(BaseModel):
    """An image attached to a message.

    ``data`` holds base64 without a prefix and may be empty when the part only
    references a file on disk (``source_path``); adapters load it lazily.
    """

    media_type: str
    data: str = ""
    source_path: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None

    @property
    def is_loaded(self) -> bool:
        return bool(self.data)

    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"

    def describe(self) -> str:
        size = (
            f"{self.size_bytes} bytes"
            if self.size_bytes is not None
            else "unknown size"
        )
        dimensions = (
            f", {self.width}x{self.height}" if self.width and self.height else ""
        )
        origin = f" from {self.source_path}" if self.source_path else ""
        return f"[image {self.media_type}, {size}{dimensions}]{origin}"


def message_for_storage(message: LLMMessage) -> LLMMessage:
    """Return a copy safe to persist: image bytes are dropped, refs are kept."""
    if not message.images:
        return message
    return message.model_copy(
        update={
            "images": [part.model_copy(update={"data": ""}) for part in message.images]
        }
    )


class Role(StrEnum):
    system = auto()
    user = auto()
    assistant = auto()
    tool = auto()


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    content: Content | None = None
    reasoning_content: str | None = None  # For thinking/reasoning tokens
    thinking_signature: str | None = (
        None  # Signature for thinking blocks (required by API)
    )
    tool_calls: list[ToolCall] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    images: list[ImagePart] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_any(cls, v: Any) -> dict[str, Any] | Any:
        if isinstance(v, dict):
            v.setdefault("content", "")
            # Handle both missing role AND role=None (some providers return null in streaming deltas)
            if v.get("role") is None:
                v["role"] = "assistant"
            return v
        role = getattr(v, "role", None)
        return {
            "role": str(role) if role is not None else "assistant",
            "content": getattr(v, "content", ""),
            "reasoning_content": getattr(v, "reasoning_content", None),
            "thinking_signature": getattr(v, "thinking_signature", None),
            "tool_calls": getattr(v, "tool_calls", None),
            "name": getattr(v, "name", None),
            "tool_call_id": getattr(v, "tool_call_id", None),
        }


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMChunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    message: LLMMessage
    finish_reason: str | None = None
    usage: LLMUsage | None = None


class BaseEvent(BaseModel, ABC):
    """Abstract base class for all agent events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class AssistantEvent(BaseEvent):
    content: str
    reasoning_content: str | None = None  # For thinking/reasoning tokens
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    session_total_tokens: int = 0
    last_turn_duration: float = 0.0
    tokens_per_second: float = 0.0
    stopped_by_middleware: bool = False


class ToolCallEvent(BaseEvent):
    tool_name: str
    tool_class: type[BaseTool]
    args: BaseModel
    tool_call_id: str


class ToolResultEvent(BaseEvent):
    tool_name: str
    tool_class: type[BaseTool] | None
    result: BaseModel | None = None
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    duration: float | None = None
    tool_call_id: str


class CompactStartEvent(BaseEvent):
    current_context_tokens: int
    threshold: int


class CompactEndEvent(BaseEvent):
    old_context_tokens: int
    new_context_tokens: int
    summary_length: int


class ContextUsageEvent(BaseEvent):
    context_tokens: int
    context_window: int
    context_percent: float


class ModeChangedEvent(BaseEvent):
    """Emitted when agent mode changes via tool."""

    new_mode: AgentMode


class SystemPromptChangedEvent(BaseEvent):
    """Emitted when the rendered system prompt content changes."""

    content: str


class MCPToolsChangedEvent(BaseEvent):
    """Emitted when an MCP server reports a changed tool list."""

    server: str | None = None


class SubagentEvent(BaseEvent):
    """Coarse progress summary for a delegated subagent run."""

    description: str
    status: Literal["running", "completed", "failed", "timeout", "cancelled"] = (
        "running"
    )
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    message: str | None = None


type SubagentProgressEvent = SubagentEvent | ToolCallEvent | ToolResultEvent


class OutputFormat(StrEnum):
    TEXT = auto()
    JSON = auto()
    STREAMING = auto()
    VSCODE = auto()  # Structured streaming events for VSCode extension
    EVENTS = auto()  # Versioned JSONL event stream (documented in docs/cli-events.md)


class StreamEventType(StrEnum):
    """Event types for JSON streaming output (used by VSCode extension)."""

    THREAD_STARTED = "thread.started"
    ITEM_STARTED = "item.started"
    ITEM_UPDATED = "item.updated"
    ITEM_COMPLETED = "item.completed"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_APPROVAL_REQUIRED = "tool.approval_required"
    PLAN_APPROVAL_REQUIRED = "plan.approval_required"
    ERROR = "error"
    THINKING_STARTED = "thinking.started"
    THINKING_UPDATED = "thinking.updated"
    THINKING_COMPLETED = "thinking.completed"
    SESSION_STARTED = "session.started"
    MESSAGE_DELTA = "message.delta"
    SESSION_COMPACTED = "session.compacted"


class StreamEvent(BaseModel):
    """Base model for JSON streaming events."""

    type: StreamEventType
    timestamp: str | None = None


type AsyncApprovalCallback = Callable[
    [str, dict[str, Any], str], Awaitable[tuple[str, str | None]]
]

type SyncApprovalCallback = Callable[[str, dict[str, Any], str], tuple[str, str | None]]

type ApprovalCallback = AsyncApprovalCallback | SyncApprovalCallback
