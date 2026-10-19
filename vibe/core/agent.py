from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from vibe.core.audit import AuditDecision, AuditLogger, build_audit_logger
from vibe.core.config import ModelConfig, ThinkingLevel, VibeConfig
from vibe.core.context.builder import (
    BuildResult,
    ContextBuilder,
    ContextOverview,
    EditAction,
    pending_tool_calls,
    placeholder_response,
)
from vibe.core.context.compactor import CompactionReason, Compactor
from vibe.core.context.overflow import is_context_overflow
from vibe.core.context.tokens import estimate_context_tokens
from vibe.core.hooks.events import (
    AfterResponseEvent,
    AgentEndEvent,
    AgentStartEvent,
    BeforeRequestEvent,
    HookDecision,
    ToolCallEventHook,
    ToolResultEventHook,
)
from vibe.core.hooks.loader import load_hooks
from vibe.core.hooks.registry import HookRegistry
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.llm.backend.factory import BACKEND_FACTORY
from vibe.core.llm.cache import CachePlan, build_cache_plan
from vibe.core.llm.format import (
    APIToolFormatHandler,
    FailedToolCall,
    ResolvedMessage,
    ResolvedToolCall,
)
from vibe.core.llm.session_headers import session_headers
from vibe.core.llm.types import BackendLike
from vibe.core.mcp.manager import MCPManager
from vibe.core.metrics import (
    COUNTER_COMPACTIONS,
    COUNTER_OVERFLOWS,
    COUNTER_TOOL_FAILURES,
    COUNTER_TURNS,
    DebugLogger,
    MetricsRecorder,
    build_debug_logger,
    build_metrics_recorder,
)
from vibe.core.middleware import (
    AutoCompactMiddleware,
    ContextWarningMiddleware,
    ConversationContext,
    MiddlewareAction,
    MiddlewarePipeline,
    MiddlewareResult,
    PriceLimitMiddleware,
    TurnLimitMiddleware,
)
from vibe.core.prompts import UtilityPrompt
from vibe.core.prompts.sections import PromptSection
from vibe.core.sessions.entries import (
    ContextEditEntry,
    CustomEntry,
    HeadMarkerEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    ThinkingLevelChangeEntry,
    UsageEntry,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.recorder import SessionRecorder
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.skills.manager import SkillManager
from vibe.core.system_prompt import (
    build_additional_contexts,
    build_prompt_sections,
    collect_tool_prompts,
)
from vibe.core.tools.base import (
    BaseTool,
    ToolError,
    ToolPermission,
    ToolPermissionError,
)
from vibe.core.tools.manager import ToolManager
from vibe.core.types import (
    AgentMode,
    AgentStats,
    ApprovalCallback,
    AssistantEvent,
    BaseEvent,
    CompactEndEvent,
    CompactStartEvent,
    ContextUsageEvent,
    ImagePart,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    MCPToolsChangedEvent,
    ModeChangedEvent,
    Role,
    SubagentEvent,
    SubagentProgressEvent,
    SyncApprovalCallback,
    SystemPromptChangedEvent,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
)
from vibe.core.utils import (
    TOOL_ERROR_TAG,
    VIBE_STOP_EVENT_TAG,
    ApprovalResponse,
    CancellationReason,
    get_user_agent,
    get_user_cancellation_message,
    is_user_cancellation_event,
    logger,
)

if TYPE_CHECKING:
    from vibe.core.subagent import SubAgentRunner


# Tools allowed in Plan Mode (read-only operations + plan submission)
# Note: exit_plan_mode is NOT included because mode switching happens
# automatically through submit_plan's approval callback
PLAN_MODE_ALLOWED_TOOLS = frozenset({
    "read_file",
    "grep",
    "find",
    "ls",
    "todo",
    "submit_plan",
    "bash",
    "skill",
    "run_tools_script",
})


class ToolExecutionResponse(StrEnum):
    SKIP = auto()
    EXECUTE = auto()


class ToolDecision(BaseModel):
    verdict: ToolExecutionResponse
    feedback: str | None = None


@dataclass
class _PlanApproval:
    approved: bool
    mode_or_feedback: str | None


@dataclass
class _ApprovedCall:
    tool_call: ResolvedToolCall
    tool_instance: BaseTool
    tool_call_id: str
    timeout: float | None
    duration: float | None = None
    plan_approval: _PlanApproval | None = None


@dataclass
class _SkippedCall:
    tool_call: ResolvedToolCall
    tool_call_id: str
    reason: str


@dataclass
class _FailedPreparedCall:
    tool_call: ResolvedToolCall
    tool_call_id: str
    error_msg: str


_PreparedCall = _ApprovedCall | _SkippedCall | _FailedPreparedCall


class AgentError(Exception):
    """Base exception for Agent errors."""


class AgentStateError(AgentError):
    """Raised when agent is in an invalid state."""


class LLMResponseError(AgentError):
    """Raised when LLM response is malformed or missing expected data."""


def _mode_string_to_agent_mode(mode_string: str) -> AgentMode | None:
    """Convert mode string from plan approval to AgentMode enum."""
    match mode_string:
        case "interactive":
            return AgentMode.INTERACTIVE
        case "auto-approve" | "auto_approve":
            return AgentMode.AUTO_APPROVE
        case _:
            return None


class Agent:
    audit_logger: AuditLogger | None
    metrics: MetricsRecorder | None
    debug_logger: DebugLogger | None

    def __init__(
        self,
        config: VibeConfig,
        auto_approve: bool = False,
        mode: AgentMode | None = None,
        message_observer: Callable[[LLMMessage], None] | None = None,
        max_turns: int | None = None,
        max_price: float | None = None,
        backend: BackendLike | None = None,
        enable_streaming: bool = False,
        session_id: str | None = None,
        additional_directory_contexts: list[Path] | None = None,
        mcp_manager: MCPManager | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        self.config = config

        # Initialize skill manager for domain-specific expertise
        self.skill_manager = SkillManager(config)

        # Initialize tool manager with skill manager reference. When an MCP
        # manager is provided its already-discovered tools are registered;
        # otherwise ToolManager keeps the legacy connect-per-call behaviour.
        self.mcp_manager = mcp_manager
        self.tool_manager = ToolManager(
            config,
            skill_manager=self.skill_manager,
            mcp_manager=mcp_manager,
            tools_changed_callback=self._on_mcp_tools_changed,
        )
        self.format_handler = APIToolFormatHandler()

        # Lifecycle extension hooks (project + package + global directories)
        self.hooks: HookRegistry = load_hooks(config)

        self.backend_factory = lambda: backend or self._select_backend()
        self.backend = self.backend_factory()

        self.message_observer = message_observer
        self._last_observed_message_index: int = 0
        self.middleware_pipeline = MiddlewarePipeline()
        self.enable_streaming = enable_streaming
        self._setup_middleware(max_turns, max_price)

        # Support both mode and legacy auto_approve parameter - set mode before system prompt
        if mode is not None:
            self._mode = mode
        elif auto_approve:
            self._mode = AgentMode.AUTO_APPROVE
        else:
            self._mode = AgentMode.INTERACTIVE

        self._additional_directory_contexts: list[Path] = list(
            additional_directory_contexts or []
        )

        self._prompt_sections = build_prompt_sections(
            self.tool_manager,
            config,
            skill_manager=self.skill_manager,
            mode=self._mode.value,
            additional_directory_contexts=self._additional_directory_contexts,
        )

        self._set_system_message(self._prompt_sections.render())

        self.stats = AgentStats()
        try:
            active_model = config.get_active_model()
            self.stats.update_pricing(active_model.pricing)
            self.stats.context_window = active_model.context_window
        except ValueError:
            pass

        self._last_usage: LLMUsage | None = None
        self._overflow_recovered = False
        self._thinking_level: ThinkingLevel | None = None
        self.compactor = Compactor(self)
        self._steering: asyncio.Queue[str] = asyncio.Queue()
        self._abort_event = asyncio.Event()
        self._active_task: asyncio.Task[Any] | None = None
        self._pending_events: list[BaseEvent] = []

        self.approval_callback: ApprovalCallback | None = None
        self.plan_approval_callback: Callable[[str], Any] | None = (
            None  # Callback for submit_plan approval
        )

        self.session_id = session_id or str(uuid4())

        # Find existing session file if resuming to update it instead of creating new
        existing_filepath = None
        if session_id:
            existing_filepath = InteractionLogger.find_session_by_id(
                session_id, config.session_logging
            )

        self.interaction_logger = InteractionLogger(
            config.session_logging,
            self.session_id,
            self._mode == AgentMode.AUTO_APPROVE,
            config.effective_workdir,
            existing_filepath=existing_filepath,
            parent_session_id=parent_session_id,
        )
        self._recorder = SessionRecorder(self.interaction_logger, agent=self)
        self._log = self._read_session_entries(existing_filepath)
        self._observe_system_message()

        self._init_observability(config)

        self._last_chunk: LLMChunk | None = None

    def _init_observability(self, config: VibeConfig) -> None:
        self.audit_logger = build_audit_logger(config.audit, session_id=self.session_id)
        self.metrics = build_metrics_recorder(config.telemetry)
        self.debug_logger = build_debug_logger(config.debug)
        self._subagent_event_queue: asyncio.Queue[SubagentProgressEvent] = (
            asyncio.Queue()
        )
        self._subagent_runner: SubAgentRunner | None = None

    @property
    def mode(self) -> AgentMode:
        """Get the current agent mode."""
        return self._mode

    @mode.setter
    def mode(self, value: AgentMode) -> None:
        """Set the agent mode and regenerate system prompt if mode changed."""
        if self._mode != value:
            self._mode = value
            self._regenerate_system_prompt_for_mode()

    @property
    def auto_approve(self) -> bool:
        """Backward compatibility: returns True if mode is AUTO_APPROVE."""
        return self._mode == AgentMode.AUTO_APPROVE

    @auto_approve.setter
    def auto_approve(self, value: bool) -> None:
        """Backward compatibility: sets mode to AUTO_APPROVE or INTERACTIVE."""
        if value:
            self._mode = AgentMode.AUTO_APPROVE
        elif self._mode == AgentMode.AUTO_APPROVE:
            self._mode = AgentMode.INTERACTIVE

    def _select_backend(self) -> BackendLike:
        active_model = self.config.get_active_model()
        provider = self.config.get_provider_for_model(active_model)
        timeout = (
            provider.timeout
            if provider.timeout is not None
            else self.config.api_timeout
        )
        return BACKEND_FACTORY[provider.backend](
            provider=provider,
            timeout=timeout,
            honor_retry_after=self.config.retry.honor_retry_after,
        )

    def set_thinking_level(
        self, level: ThinkingLevel | None, *, record: bool = False
    ) -> None:
        """Set the session thinking level override (None clears it).

        When ``record`` is true a ``thinking_level_change`` entry and a matching
        custom entry are appended so resume can restore the choice.
        """
        self._thinking_level = level
        if record:
            value = level.value if level is not None else None
            self._append_entry(
                ThinkingLevelChangeEntry(
                    id=new_entry_id(),
                    parent_id=self._last_entry_id(),
                    timestamp=now_iso(),
                    level=value,
                )
            )
            self.record_custom("thinking_level", {"level": value})

    @property
    def thinking_level(self) -> ThinkingLevel | None:
        return self._thinking_level

    def record_custom(self, key: str, value: dict[str, Any]) -> None:
        """Persist an arbitrary ``CustomEntry`` on the session store."""
        self._append_entry(
            CustomEntry(
                id=new_entry_id(),
                parent_id=self._last_entry_id(),
                timestamp=now_iso(),
                key=key,
                value=value,
            )
        )

    def set_model(self, alias: str) -> ModelConfig:
        """Switch the active model and rebuild the backend.

        Records a ``ModelChangeEntry`` plus a ``model`` custom entry so the
        choice can be restored on resume.

        Raises:
            ValueError: If ``alias`` is not a configured model.
        """
        model = self.config.get_model_by_alias(alias)
        if self.config.active_model == alias:
            return model

        self.config.active_model = alias
        self.stats.update_pricing(model.pricing)
        self.stats.context_window = model.context_window
        self.backend = self.backend_factory()
        self._record_model_change(alias, model.provider)
        self.record_custom("model", {"alias": alias, "provider": model.provider})

        if self.config.include_model_info:
            self._prompt_sections.set(
                PromptSection(
                    "model_info", f"Your model name is: `{alias}`", tagged=False
                )
            )
            self._apply_prompt_sections()
        return model

    def steer(self, text: str) -> None:
        """Queue a user message to continue the turn without waiting for input."""
        if text.strip():
            self._steering.put_nowait(text)

    def abort(self) -> None:
        """Cancel the running turn as soon as possible."""
        self._abort_event.set()
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()

    @property
    def aborted(self) -> bool:
        return self._abort_event.is_set()

    def _get_or_create_subagent_runner(self) -> SubAgentRunner:
        from vibe.core.subagent import SubAgentRunner

        if self._subagent_runner is None:
            self._subagent_runner = SubAgentRunner(self)
        return self._subagent_runner

    def _emit_subagent_event(self, event: SubagentProgressEvent) -> None:
        self._subagent_event_queue.put_nowait(event)

    def _drain_steering(self) -> list[str]:
        messages: list[str] = []
        while not self._steering.empty():
            messages.append(self._steering.get_nowait())
        return messages

    @property
    def messages(self) -> list[LLMMessage]:
        """Conversation sent to the model, derived from the session entry log.

        Change the conversation through ``add_message``, ``edit_context``,
        ``reset_context`` or compaction. Edits made to this list are not kept.
        """
        return self._context_result().messages

    def _context_result(self) -> BuildResult:
        context = self._context
        tail_id = self._log[-1].id if self._log else None
        if context is None:
            context = self._context_builder.build(self._log)
        elif context.last_entry_id != tail_id:
            context = self._context_builder.build_from(self._log, context)
        self._context = context
        return context

    def _last_entry_id(self) -> str | None:
        """Parent id for the next entry: the session header id for an empty log."""
        return self._log[-1].id if self._log else self.session_id

    def _entry_id_at(self, message_index: int) -> str | None:
        return self._context_result().entry_id_at(message_index)

    def _append_entry(self, entry: SessionEntry) -> None:
        self._log.append(entry)
        self._recorder.write(entry)

    def _set_system_message(self, content: str) -> None:
        self._system_message = LLMMessage(role=Role.system, content=content)
        self._context_builder = ContextBuilder(self._system_message)
        self._context = None

    def _observe_system_message(self) -> None:
        if self.message_observer:
            self.message_observer(self.messages[0])
            self._last_observed_message_index = 1

    def add_message(self, message: LLMMessage) -> None:
        self._append_message(message)

    def _append_message(self, message: LLMMessage) -> None:
        self._append_entry(
            MessageEntry(
                id=new_entry_id(),
                parent_id=self._last_entry_id(),
                timestamp=now_iso(),
                message=message,
            )
        )

    def _record_model_change(self, alias: str, provider: str) -> None:
        self._append_entry(
            ModelChangeEntry(
                id=new_entry_id(),
                parent_id=self._last_entry_id(),
                timestamp=now_iso(),
                model_alias=alias,
                provider=provider,
            )
        )

    def _read_session_entries(self, filepath: Path | None) -> list[SessionEntry]:
        if filepath is None or not self.interaction_logger.writes_entries:
            return []
        store = JsonlSessionStore(self.config.session_logging)
        return list(store.read(filepath).entries)

    def _sync_session_log(self) -> None:
        """Append entries that another writer added to the session file."""
        filepath = self.interaction_logger.filepath
        if filepath is None or not self.interaction_logger.writes_entries:
            return
        store = JsonlSessionStore(self.config.session_logging)
        tail = store.read_since(filepath, self._last_entry_id())
        if tail.anchor_found:
            self._log.extend(tail.entries)

    def _flush_new_messages(self) -> None:
        if not self.message_observer:
            return

        if self._last_observed_message_index >= len(self.messages):
            return

        for msg in self.messages[self._last_observed_message_index :]:
            self.message_observer(msg)
        self._last_observed_message_index = len(self.messages)

    async def act(self, msg: str) -> AsyncGenerator[BaseEvent]:
        self._sync_session_log()
        self._clean_message_history()
        self._abort_event.clear()
        self._active_task = asyncio.current_task()
        start_decision = await self.hooks.dispatch(
            AgentStartEvent(session_id=self.session_id, prompt=msg)
        )
        for injected in start_decision.injected_messages:
            self._append_message(injected)
        while self._pending_events:
            yield self._pending_events.pop(0)
        try:
            async for event in self._conversation_loop(msg):
                yield event
        finally:
            self._active_task = None

    def _setup_middleware(self, max_turns: int | None, max_price: float | None) -> None:
        self.middleware_pipeline.clear()

        if max_turns is not None:
            self.middleware_pipeline.add(TurnLimitMiddleware(max_turns))

        if max_price is not None:
            self.middleware_pipeline.add(PriceLimitMiddleware(max_price))

        context_limit = self.config.get_context_limit_for_active_model()
        if context_limit > 0 and self.config.compaction.enabled:
            self.middleware_pipeline.add(
                AutoCompactMiddleware(self.config.get_compaction_threshold())
            )
        if context_limit > 0 and self.config.context_warnings:
            self.middleware_pipeline.add(
                ContextWarningMiddleware(max_context=context_limit)
            )

    async def _handle_middleware_result(
        self, result: MiddlewareResult
    ) -> AsyncGenerator[BaseEvent]:
        match result.action:
            case MiddlewareAction.STOP:
                yield AssistantEvent(
                    content=f"<{VIBE_STOP_EVENT_TAG}>{result.reason}</{VIBE_STOP_EVENT_TAG}>",
                    prompt_tokens=0,
                    completion_tokens=0,
                    session_total_tokens=self.stats.session_total_llm_tokens,
                    last_turn_duration=0,
                    tokens_per_second=0,
                    stopped_by_middleware=True,
                )
                await self._recorder.flush()

            case MiddlewareAction.INJECT_MESSAGE:
                if result.message and len(self.messages) > 0:
                    last_msg = self.messages[-1]
                    if last_msg.content:
                        last_msg.content += f"\n\n{result.message}"
                    else:
                        last_msg.content = result.message

            case MiddlewareAction.COMPACT:
                old_tokens = result.metadata.get(
                    "old_tokens", self.stats.context_tokens
                )
                threshold = result.metadata.get(
                    "threshold", self.config.get_context_limit_for_active_model()
                )

                yield CompactStartEvent(
                    current_context_tokens=old_tokens, threshold=threshold
                )

                summary = await self.compact(
                    reason=result.metadata.get("compact_reason", "auto")
                )

                yield CompactEndEvent(
                    old_context_tokens=old_tokens,
                    new_context_tokens=self.stats.context_tokens,
                    summary_length=len(summary),
                )

            case MiddlewareAction.CONTINUE:
                pass

    def _get_context(self) -> ConversationContext:
        return ConversationContext(
            messages=self.messages, stats=self.stats, config=self.config
        )

    def _refresh_context_stats(self) -> None:
        tools = self.format_handler.get_available_tools(self.tool_manager, self.config)
        self.stats.context_tokens = estimate_context_tokens(
            self.messages, tools, self._last_usage, self.stats.last_usage_message_index
        )
        if deferred := self.tool_manager.deferred_pool():
            logger.debug(
                "Tool schema: %d visible, %d deferred", len(tools), len(deferred)
            )

    def _context_usage_event(self) -> ContextUsageEvent:
        return ContextUsageEvent(
            context_tokens=self.stats.context_tokens,
            context_window=self.stats.context_window,
            context_percent=self.stats.context_percent,
        )

    async def _recover_from_context_overflow(
        self, error: BaseException
    ) -> AsyncGenerator[BaseEvent]:
        self._record_metric(COUNTER_OVERFLOWS)
        metadata = {
            "old_tokens": self.stats.context_tokens,
            "threshold": self.config.get_compaction_threshold(),
            "compact_reason": "overflow",
        }
        try:
            async for event in self._handle_middleware_result(
                MiddlewareResult(action=MiddlewareAction.COMPACT, metadata=metadata)
            ):
                yield event
        except Exception as compact_exc:
            raise AgentError(
                "Context overflow recovery failed: could not compact the conversation. "
                f"Original error: {error}. Compaction error: {compact_exc}"
            ) from compact_exc

    async def _conversation_loop(self, user_msg: str) -> AsyncGenerator[BaseEvent]:
        try:
            async for event in self._run_conversation_loop(user_msg):
                yield event
        finally:
            await self.hooks.dispatch(
                AgentEndEvent(
                    session_id=self.session_id, usage=self._last_usage or LLMUsage()
                )
            )

    def _begin_conversation_turn(self, user_msg: str) -> None:
        self.tool_manager.reset_deferred_exposure()
        self._append_message(LLMMessage(role=Role.user, content=user_msg))
        self.stats.steps += 1
        self._record_metric(COUNTER_TURNS)
        self._overflow_recovered = False
        self.middleware_pipeline.reset()

    async def _run_conversation_loop(self, user_msg: str) -> AsyncGenerator[BaseEvent]:
        self._begin_conversation_turn(user_msg)

        should_break_loop = False
        while not should_break_loop:
            if self._abort_event.is_set():
                self._flush_new_messages()
                await self._recorder.flush()
                return
            try:
                self._refresh_context_stats()
                result = await self.middleware_pipeline.run_before_turn(
                    self._get_context()
                )

                async for event in self._handle_middleware_result(result):
                    yield event

                if result.action == MiddlewareAction.STOP:
                    self._flush_new_messages()
                    yield self._context_usage_event()
                    return

                self.stats.steps += 1
                user_cancelled = False
                async for event in self._perform_llm_turn():
                    if is_user_cancellation_event(event):
                        user_cancelled = True
                    yield event

                last_message = self.messages[-1]
                should_break_loop = (
                    last_message.role != Role.tool
                    and self._last_chunk is not None
                    and self._last_chunk.finish_reason is not None
                )

                self._flush_new_messages()
                await self._recorder.flush()

                if user_cancelled:
                    self._flush_new_messages()
                    await self._recorder.flush()
                    yield self._context_usage_event()
                    return

                after_result = await self.middleware_pipeline.run_after_turn(
                    self._get_context()
                )

                async for event in self._handle_middleware_result(after_result):
                    yield event

                if after_result.action == MiddlewareAction.STOP:
                    self._flush_new_messages()
                    yield self._context_usage_event()
                    return

                if steering := self._drain_steering():
                    for text in steering:
                        self._append_message(LLMMessage(role=Role.user, content=text))
                    should_break_loop = False

                self._flush_new_messages()
                await self._recorder.flush()

            except Exception as exc:
                if is_context_overflow(exc) and not self._overflow_recovered:
                    self._overflow_recovered = True
                    async for event in self._recover_from_context_overflow(exc):
                        yield event
                    continue

                self._flush_new_messages()
                await self._recorder.flush()
                raise

        self._flush_new_messages()
        yield self._context_usage_event()

    async def _dispatch_before_request(self) -> HookDecision:
        active_model = self.config.get_active_model()
        return await self.hooks.dispatch(
            BeforeRequestEvent(
                messages=list(self.messages), model_alias=active_model.alias
            )
        )

    def _request_messages(self, decision: HookDecision) -> list[LLMMessage] | None:
        if not decision.injected_messages:
            return None
        return [*self.messages, *decision.injected_messages]

    async def _dispatch_after_response(self) -> None:
        chunk = self._last_chunk
        if chunk is None or chunk.usage is None:
            return
        await self.hooks.dispatch(
            AfterResponseEvent(message=chunk.message, usage=chunk.usage)
        )

    async def _perform_llm_turn(
        self,
    ) -> AsyncGenerator[
        AssistantEvent
        | ToolCallEvent
        | ToolResultEvent
        | ModeChangedEvent
        | SystemPromptChangedEvent
        | SubagentEvent
    ]:
        decision = await self._dispatch_before_request()
        if decision.action == "deny":
            reason = decision.reason or "no reason provided"
            content = f"Request blocked by hook: {reason}"
            denial = LLMMessage(role=Role.assistant, content=content)
            self._append_message(denial)
            self._last_chunk = LLMChunk(
                message=denial, usage=None, finish_reason="stop"
            )
            yield AssistantEvent(
                content=content,
                session_total_tokens=self.stats.session_total_llm_tokens,
                last_turn_duration=self.stats.last_turn_duration,
                tokens_per_second=self.stats.tokens_per_second,
            )
            return

        request_messages = self._request_messages(decision)
        if self.enable_streaming:
            async for event in self._stream_assistant_events(request_messages):
                yield event
        else:
            assistant_event = await self._get_assistant_event(request_messages)
            if assistant_event.content:
                yield assistant_event

        await self._dispatch_after_response()

        self._log_request_debug(request_messages)

        last_message = self.messages[-1]
        last_chunk = self._last_chunk
        if last_chunk is None or last_chunk.usage is None:
            raise LLMResponseError("LLM response missing chunk or usage data")

        parsed = self.format_handler.parse_message(last_message)
        resolved = self.format_handler.resolve_tool_calls(
            parsed, self.tool_manager, self.config
        )

        if last_chunk.usage.completion_tokens > 0 and self.stats.last_turn_duration > 0:
            self.stats.tokens_per_second = (
                last_chunk.usage.completion_tokens / self.stats.last_turn_duration
            )

        if not resolved.tool_calls and not resolved.failed_calls:
            return

        async for event in self._handle_tool_calls(resolved):
            yield event

    def _create_assistant_event(
        self, content: str, chunk: LLMChunk | None, reasoning_content: str | None = None
    ) -> AssistantEvent:
        usage = chunk.usage if chunk else None
        return AssistantEvent(
            content=content,
            reasoning_content=reasoning_content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cost_usd=self.stats.usage_cost(usage) if usage else 0.0,
            session_total_tokens=self.stats.session_total_llm_tokens,
            last_turn_duration=self.stats.last_turn_duration,
            tokens_per_second=self.stats.tokens_per_second,
        )

    async def _stream_assistant_events(
        self, messages: list[LLMMessage] | None = None
    ) -> AsyncGenerator[AssistantEvent]:
        chunks: list[LLMChunk] = []
        content_buffer = ""
        reasoning_buffer = ""
        chunks_with_content = 0
        BATCH_SIZE = 5

        async for chunk in self._chat_streaming(messages=messages):
            chunks.append(chunk)

            # Handle reasoning/thinking content
            if chunk.message.reasoning_content:
                reasoning_buffer += chunk.message.reasoning_content
                # Yield reasoning content immediately (don't batch)
                yield self._create_assistant_event(
                    "", chunk, reasoning_content=reasoning_buffer
                )
                reasoning_buffer = ""
                continue

            if chunk.message.tool_calls and chunk.finish_reason is None:
                if chunk.message.content:
                    content_buffer += chunk.message.content
                    chunks_with_content += 1

                if content_buffer:
                    yield self._create_assistant_event(content_buffer, chunk)
                    content_buffer = ""
                    chunks_with_content = 0
                continue

            if chunk.message.content:
                content_buffer += chunk.message.content
                chunks_with_content += 1

                if chunks_with_content >= BATCH_SIZE:
                    yield self._create_assistant_event(content_buffer, chunk)
                    content_buffer = ""
                    chunks_with_content = 0

        if content_buffer:
            last_chunk = chunks[-1] if chunks else None
            yield self._create_assistant_event(content_buffer, last_chunk)

        last_message = self._merge_streamed_chunks(chunks)
        self._append_message(last_message)
        finish_reason = next(
            (c.finish_reason for c in chunks if c.finish_reason is not None), None
        )
        self._finalize_assistant_stats(last_message, chunks[-1].usage, finish_reason)

    @staticmethod
    def _merge_streamed_chunks(chunks: list[LLMChunk]) -> LLMMessage:
        content = "".join(chunk.message.content or "" for chunk in chunks)
        reasoning = "".join(chunk.message.reasoning_content or "" for chunk in chunks)
        signature = "".join(chunk.message.thinking_signature or "" for chunk in chunks)
        return LLMMessage(
            role=Role.assistant,
            content=content,
            reasoning_content=reasoning or None,
            thinking_signature=signature or None,
            tool_calls=Agent._merge_tool_calls(chunks),
        )

    @staticmethod
    def _merge_tool_calls(chunks: list[LLMChunk]) -> list[ToolCall] | None:
        merged = OrderedDict[int, ToolCall]()
        for chunk in chunks:
            for tool_call in chunk.message.tool_calls or []:
                if tool_call.index is None:
                    raise LLMResponseError("Tool call chunk missing index")
                if tool_call.index not in merged:
                    merged[tool_call.index] = tool_call
                    continue
                merged[tool_call.index].function.arguments = (
                    merged[tool_call.index].function.arguments or ""
                ) + (tool_call.function.arguments or "")
        return list(merged.values()) or None

    def _finalize_assistant_stats(
        self, message: LLMMessage, usage: LLMUsage | None, finish_reason: str | None
    ) -> None:
        self._last_chunk = LLMChunk(
            message=message, usage=usage, finish_reason=finish_reason
        )
        self.stats.last_usage_message_index = len(self.messages) - 1
        self._refresh_context_stats()
        if usage is not None:
            self._record_usage(usage)

    def _record_usage(self, usage: LLMUsage) -> None:
        self._append_entry(
            UsageEntry(
                id=new_entry_id(),
                parent_id=self._last_entry_id(),
                timestamp=now_iso(),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=self.stats.usage_cost(usage),
            )
        )

    async def _get_assistant_event(
        self, messages: list[LLMMessage] | None = None
    ) -> AssistantEvent:
        llm_result = await self._chat(messages=messages)
        if llm_result.usage is None:
            raise LLMResponseError(
                "Usage data missing in non-streaming completion response"
            )
        self._last_chunk = llm_result
        assistant_msg = llm_result.message
        self._append_message(assistant_msg)
        self._finalize_assistant_stats(
            assistant_msg, llm_result.usage, llm_result.finish_reason
        )
        return AssistantEvent(
            content=assistant_msg.content or "",
            prompt_tokens=llm_result.usage.prompt_tokens,
            completion_tokens=llm_result.usage.completion_tokens,
            cache_read_tokens=llm_result.usage.cache_read_tokens,
            cache_write_tokens=llm_result.usage.cache_write_tokens,
            cost_usd=self.stats.usage_cost(llm_result.usage),
            session_total_tokens=self.stats.session_total_llm_tokens,
            last_turn_duration=self.stats.last_turn_duration,
            tokens_per_second=self.stats.tokens_per_second,
        )

    async def _handle_tool_calls(
        self, resolved: ResolvedMessage
    ) -> AsyncGenerator[
        ToolCallEvent
        | ToolResultEvent
        | ModeChangedEvent
        | SystemPromptChangedEvent
        | SubagentEvent
    ]:
        async for event in self._emit_failed_tool_calls(resolved.failed_calls):
            yield event

        prepared: list[_PreparedCall] = []
        for tool_call in resolved.tool_calls:
            tool_call_id = tool_call.call_id

            yield ToolCallEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                args=tool_call.validated_args,
                tool_call_id=tool_call_id,
            )

            try:
                tool_instance = self._get_tool_instance(tool_call.tool_name)
            except Exception as exc:
                prepared.append(
                    _FailedPreparedCall(
                        tool_call=tool_call,
                        tool_call_id=tool_call_id,
                        error_msg=f"Error getting tool '{tool_call.tool_name}': {exc}",
                    )
                )
                continue

            hook_decision = await self.hooks.dispatch(
                ToolCallEventHook(
                    tool_name=tool_call.tool_name,
                    tool_call_id=tool_call_id,
                    args=dict(tool_call.args_dict),
                )
            )
            if hook_decision.action == "deny":
                prepared.append(
                    _SkippedCall(
                        tool_call=tool_call,
                        tool_call_id=tool_call_id,
                        reason=(
                            hook_decision.reason
                            or f"Tool call blocked by hook: {tool_call.tool_name}"
                        ),
                    )
                )
                continue

            if (
                hook_decision.action == "modify"
                and hook_decision.replacement_args is not None
            ):
                try:
                    tool_call = self._apply_hook_args(
                        tool_call, hook_decision.replacement_args
                    )
                except ValidationError as exc:
                    prepared.append(
                        _FailedPreparedCall(
                            tool_call=tool_call,
                            tool_call_id=tool_call_id,
                            error_msg=(
                                f"Hook replacement args invalid for tool "
                                f"'{tool_call.tool_name}': {exc}"
                            ),
                        )
                    )
                    continue

            decision = await self._should_execute_tool(
                tool_instance, tool_call.args_dict, tool_call_id
            )

            if decision.verdict == ToolExecutionResponse.SKIP:
                skip_reason = decision.feedback or str(
                    get_user_cancellation_message(
                        CancellationReason.TOOL_SKIPPED, tool_call.tool_name
                    )
                )
                prepared.append(
                    _SkippedCall(
                        tool_call=tool_call,
                        tool_call_id=tool_call_id,
                        reason=skip_reason,
                    )
                )
                continue

            plan_approval = await self._request_plan_approval(tool_call)
            prepared.append(
                _ApprovedCall(
                    tool_call=tool_call,
                    tool_instance=tool_instance,
                    tool_call_id=tool_call_id,
                    timeout=tool_instance.config.timeout,
                    plan_approval=plan_approval,
                )
            )

        outcomes: list[BaseModel | BaseException | None] = []
        async for event in self._execute_calls_streaming(prepared, outcomes):
            yield event
        async for event in self._commit_tool_results(prepared, outcomes):
            yield event

    async def _execute_calls_streaming(
        self,
        prepared: list[_PreparedCall],
        outcomes: list[BaseModel | BaseException | None],
    ) -> AsyncGenerator[SubagentProgressEvent]:
        """Run the prepared tool batch, yielding subagent progress events.

        Tools themselves cannot yield into the agent generator, so the
        subagent runner pushes summary events onto ``_subagent_event_queue``
        while the batch executes; this method interleaves them with the
        running batch and stores the final outcomes in ``outcomes``.
        """
        execution = asyncio.create_task(self._execute_prepared_calls(prepared))
        try:
            while not execution.done():
                event_task = asyncio.create_task(self._subagent_event_queue.get())
                done, _ = await asyncio.wait(
                    {execution, event_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if event_task in done:
                    yield event_task.result()
                else:
                    event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await event_task
        except BaseException:
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            raise

        outcomes.extend(execution.result())
        while not self._subagent_event_queue.empty():
            yield self._subagent_event_queue.get_nowait()

    async def _emit_failed_tool_calls(
        self, failed_calls: list[FailedToolCall]
    ) -> AsyncGenerator[ToolResultEvent]:
        for failed in failed_calls:
            error_msg = f"<{TOOL_ERROR_TAG}>{failed.tool_name}: {failed.error}</{TOOL_ERROR_TAG}>"

            yield ToolResultEvent(
                tool_name=failed.tool_name,
                tool_class=None,
                error=error_msg,
                tool_call_id=failed.call_id,
            )

            self.stats.tool_calls_failed += 1
            self._record_metric(COUNTER_TOOL_FAILURES)
            self._append_message(
                self.format_handler.create_failed_tool_response_message(
                    failed, error_msg
                )
            )

    def _apply_hook_args(
        self, tool_call: ResolvedToolCall, replacement_args: dict[str, Any]
    ) -> ResolvedToolCall:
        """Re-validate hook-provided arguments against the tool args model."""
        args_model, _ = tool_call.tool_class._get_tool_args_results()
        validated = args_model.model_validate(replacement_args)
        return tool_call.model_copy(update={"validated_args": validated})

    async def _observe_tool_result(
        self,
        tool_name: str,
        tool_call_id: str,
        *,
        ok: bool,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.hooks.dispatch(
            ToolResultEventHook(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                ok=ok,
                text=text,
                error=error,
            )
        )

    async def _request_plan_approval(
        self, tool_call: ResolvedToolCall
    ) -> _PlanApproval | None:
        if tool_call.tool_name != "submit_plan" or self.plan_approval_callback is None:
            return None

        plan_content = tool_call.args_dict.get("plan", "")
        callback_result = self.plan_approval_callback(plan_content)
        if asyncio.iscoroutine(callback_result):
            approved, mode_or_feedback = await callback_result
        else:
            approved, mode_or_feedback = callback_result
        return _PlanApproval(approved=approved, mode_or_feedback=mode_or_feedback)

    async def _execute_prepared_calls(
        self, prepared: list[_PreparedCall]
    ) -> list[BaseModel | BaseException | None]:
        outcomes: list[BaseModel | BaseException | None] = [None] * len(prepared)
        runtime = self.config.tool_runtime
        semaphore = asyncio.Semaphore(max(1, runtime.max_parallel_tools))
        batch: list[tuple[int, _ApprovedCall]] = []

        for index, call in enumerate(prepared):
            if not isinstance(call, _ApprovedCall):
                continue

            if not runtime.parallel or self._is_sequential_call(call):
                await self._run_parallel_batch(batch, outcomes, semaphore)
                outcomes[index] = await self._invoke_tool_call(call, semaphore)
            else:
                batch.append((index, call))

        await self._run_parallel_batch(batch, outcomes, semaphore)
        return outcomes

    async def _run_parallel_batch(
        self,
        batch: list[tuple[int, _ApprovedCall]],
        outcomes: list[BaseModel | BaseException | None],
        semaphore: asyncio.Semaphore,
    ) -> None:
        if not batch:
            return

        results = await asyncio.gather(
            *(self._invoke_tool_call(call, semaphore) for _, call in batch)
        )
        for (index, _), result in zip(batch, results, strict=True):
            outcomes[index] = result
        batch.clear()

    def _is_sequential_call(self, call: _ApprovedCall) -> bool:
        if call.tool_instance.execution_mode == "sequential":
            return True
        if call.tool_call.tool_name in self.config.tool_runtime.sequential:
            return True
        try:
            return not self.config.get_active_model().capabilities.parallel_tool_calls
        except ValueError:
            return False

    async def _invoke_tool_call(
        self, call: _ApprovedCall, semaphore: asyncio.Semaphore
    ) -> BaseModel | BaseException:
        start_time = time.perf_counter()
        try:
            async with semaphore:
                result = await self._invoke_with_timeout(
                    call.tool_instance, call.tool_call.args_dict, call.timeout
                )
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return exc
        except BaseException as exc:
            return exc

        call.duration = time.perf_counter() - start_time
        return result

    async def _invoke_with_timeout(
        self, tool: BaseTool, args: dict[str, Any], timeout: float | None
    ) -> BaseModel:
        if timeout is None or timeout <= 0:
            return await tool.invoke(**args)
        try:
            return await asyncio.wait_for(tool.invoke(**args), timeout=timeout)
        except TimeoutError as exc:
            raise ToolError(f"Tool timed out after {timeout}s") from exc

    async def _commit_tool_results(
        self,
        prepared: list[_PreparedCall],
        outcomes: list[BaseModel | BaseException | None],
    ) -> AsyncGenerator[ToolResultEvent | ModeChangedEvent | SystemPromptChangedEvent]:
        for item, outcome in zip(prepared, outcomes, strict=True):
            match item:
                case _SkippedCall():
                    self.stats.tool_calls_rejected += 1

                    await self._observe_tool_result(
                        item.tool_call.tool_name,
                        item.tool_call_id,
                        ok=False,
                        error=item.reason,
                    )

                    yield ToolResultEvent(
                        tool_name=item.tool_call.tool_name,
                        tool_class=item.tool_call.tool_class,
                        skipped=True,
                        skip_reason=item.reason,
                        tool_call_id=item.tool_call_id,
                    )

                    self._append_message(
                        LLMMessage.model_validate(
                            self.format_handler.create_tool_response_message(
                                item.tool_call, item.reason
                            )
                        )
                    )

                case _FailedPreparedCall():
                    await self._observe_tool_result(
                        item.tool_call.tool_name,
                        item.tool_call_id,
                        ok=False,
                        error=item.error_msg,
                    )

                    yield ToolResultEvent(
                        tool_name=item.tool_call.tool_name,
                        tool_class=item.tool_call.tool_class,
                        error=item.error_msg,
                        tool_call_id=item.tool_call_id,
                    )

                    self._append_message(
                        LLMMessage.model_validate(
                            self.format_handler.create_tool_response_message(
                                item.tool_call, item.error_msg
                            )
                        )
                    )

                case _ApprovedCall():
                    async for event in self._commit_approved_call(item, outcome):
                        yield event

    async def _commit_approved_call(
        self, call: _ApprovedCall, outcome: BaseModel | BaseException | None
    ) -> AsyncGenerator[ToolResultEvent | ModeChangedEvent | SystemPromptChangedEvent]:
        self.stats.tool_calls_agreed += 1

        if isinstance(outcome, (asyncio.CancelledError, KeyboardInterrupt)):
            cancel = str(
                get_user_cancellation_message(CancellationReason.TOOL_INTERRUPTED)
            )
            await self._observe_tool_result(
                call.tool_call.tool_name, call.tool_call_id, ok=False, error=cancel
            )
            yield ToolResultEvent(
                tool_name=call.tool_call.tool_name,
                tool_class=call.tool_call.tool_class,
                error=cancel,
                tool_call_id=call.tool_call_id,
            )
            self._append_message(
                LLMMessage.model_validate(
                    self.format_handler.create_tool_response_message(
                        call.tool_call, cancel
                    )
                )
            )
            await self._recorder.flush()
            raise outcome

        if isinstance(outcome, ToolPermissionError):
            error_msg = (
                f"<{TOOL_ERROR_TAG}>{call.tool_instance.get_name()} failed: "
                f"{outcome}</{TOOL_ERROR_TAG}>"
            )

            await self._observe_tool_result(
                call.tool_call.tool_name, call.tool_call_id, ok=False, error=error_msg
            )

            yield ToolResultEvent(
                tool_name=call.tool_call.tool_name,
                tool_class=call.tool_call.tool_class,
                error=error_msg,
                tool_call_id=call.tool_call_id,
            )

            self.stats.tool_calls_agreed -= 1
            self.stats.tool_calls_rejected += 1
            self._append_message(
                LLMMessage.model_validate(
                    self.format_handler.create_tool_response_message(
                        call.tool_call, error_msg
                    )
                )
            )
            return

        if isinstance(outcome, ToolError):
            error_msg = (
                f"<{TOOL_ERROR_TAG}>{call.tool_instance.get_name()} failed: "
                f"{outcome}</{TOOL_ERROR_TAG}>"
            )

            await self._observe_tool_result(
                call.tool_call.tool_name, call.tool_call_id, ok=False, error=error_msg
            )

            yield ToolResultEvent(
                tool_name=call.tool_call.tool_name,
                tool_class=call.tool_call.tool_class,
                error=error_msg,
                tool_call_id=call.tool_call_id,
            )

            self.stats.tool_calls_failed += 1
            self._record_metric(COUNTER_TOOL_FAILURES)
            self._append_message(
                LLMMessage.model_validate(
                    self.format_handler.create_tool_response_message(
                        call.tool_call, error_msg
                    )
                )
            )
            return

        if isinstance(outcome, BaseException):
            await self._observe_tool_result(
                call.tool_call.tool_name,
                call.tool_call_id,
                ok=False,
                error=str(outcome),
            )
            raise outcome

        if not isinstance(outcome, BaseModel):
            raise LLMResponseError(
                f"Tool '{call.tool_call.tool_name}' returned no result"
            )

        result_model = outcome
        if call.plan_approval is not None and getattr(
            result_model, "plan_submitted", False
        ):
            self._apply_plan_approval(result_model, call.plan_approval)

        text = self._format_tool_result(result_model)

        await self._observe_tool_result(
            call.tool_call.tool_name, call.tool_call_id, ok=True, text=text
        )

        self._append_message(
            LLMMessage.model_validate(
                self.format_handler.create_tool_response_message(call.tool_call, text)
            )
        )

        self._attach_result_images(result_model, call.tool_call.tool_name)

        yield ToolResultEvent(
            tool_name=call.tool_call.tool_name,
            tool_class=call.tool_call.tool_class,
            result=result_model,
            duration=call.duration,
            tool_call_id=call.tool_call_id,
        )

        self.stats.tool_calls_succeeded += 1

        execution_mode = getattr(result_model, "execution_mode", None)
        approved = getattr(result_model, "approved", None)
        if execution_mode and approved:
            new_mode = _mode_string_to_agent_mode(cast(str, execution_mode))
            if new_mode and new_mode != self._mode:
                self._mode = new_mode
                if prompt_event := self._regenerate_system_prompt_for_mode():
                    yield prompt_event
                yield ModeChangedEvent(new_mode=new_mode)

        if getattr(result_model, "mode_change", None) == "plan":
            self._mode = AgentMode.PLAN
            if prompt_event := self._regenerate_system_prompt_for_mode():
                yield prompt_event
            yield ModeChangedEvent(new_mode=AgentMode.PLAN)

    def _apply_plan_approval(
        self, result_model: BaseModel, approval: _PlanApproval
    ) -> None:
        model = cast(Any, result_model)
        model.approved = approval.approved
        if approval.approved:
            model.execution_mode = approval.mode_or_feedback
            model.message = (
                f"Plan approved. Execution mode: {approval.mode_or_feedback}"
            )
        else:
            model.execution_mode = None
            model.message = f"User requested revisions: {approval.mode_or_feedback}"

    def _format_tool_result(self, result_model: BaseModel) -> str:
        format_for_model = getattr(result_model, "format_for_model", None)
        if callable(format_for_model):
            return cast(str, format_for_model())
        return "\n".join(f"{k}: {v}" for k, v in result_model.model_dump().items())

    @staticmethod
    def _image_parts_from_result(result_model: BaseModel) -> list[ImagePart]:
        match getattr(result_model, "image", None):
            case ImagePart() as image:
                return [image]
        images = getattr(result_model, "images", None)
        if isinstance(images, list) and all(
            isinstance(image, ImagePart) for image in images
        ):
            return list(images)
        return []

    def _attach_result_images(self, result_model: BaseModel, tool_name: str) -> None:
        if images := self._image_parts_from_result(result_model):
            self._append_message(
                LLMMessage(
                    role=Role.user, content=f"Images read by {tool_name}", images=images
                )
            )

    async def _chat(
        self,
        max_tokens: int | None = None,
        *,
        messages: list[LLMMessage] | None = None,
        include_tools: bool = True,
        cache_plan: CachePlan | None = None,
    ) -> LLMChunk:
        active_model = self.config.get_active_model()
        provider = self.config.get_provider_for_model(active_model)
        request_messages = messages if messages is not None else self.messages
        if cache_plan is None:
            cache_plan = build_cache_plan(provider, active_model, self.session_id)

        send_tools = include_tools and active_model.capabilities.tools
        available_tools = (
            self.format_handler.get_available_tools(self.tool_manager, self.config)
            if send_tools
            else None
        )
        tool_choice = (
            self.format_handler.get_tool_choice()
            if send_tools and active_model.capabilities.tool_choice
            else None
        )

        try:
            start_time = time.perf_counter()

            async with self.backend as backend:
                result = await backend.complete(
                    model=active_model,
                    messages=request_messages,
                    temperature=active_model.temperature,
                    tools=available_tools,
                    tool_choice=tool_choice,
                    extra_headers={
                        "user-agent": get_user_agent(provider.backend),
                        "x-affinity": self.session_id,
                        **session_headers(provider, self.session_id),
                    },
                    max_tokens=max_tokens,
                    cache_plan=cache_plan,
                    thinking_level=self._thinking_level,
                )

            end_time = time.perf_counter()
            if result.usage is None:
                raise LLMResponseError(
                    "Usage data missing in non-streaming completion response"
                )

            self.stats.last_turn_duration = end_time - start_time
            self.stats.last_turn_prompt_tokens = result.usage.prompt_tokens
            self.stats.last_turn_completion_tokens = result.usage.completion_tokens
            self.stats.session_prompt_tokens += result.usage.prompt_tokens
            self.stats.session_completion_tokens += result.usage.completion_tokens
            self.stats.cache_read_tokens += result.usage.cache_read_tokens
            self.stats.cache_write_tokens += result.usage.cache_write_tokens
            self._last_usage = result.usage
            if messages is None:
                self.stats.last_usage_message_index = len(self.messages) - 1

            processed_message = self.format_handler.process_api_response_message(
                result.message
            )

            return LLMChunk(
                message=processed_message,
                usage=result.usage,
                finish_reason=result.finish_reason,
            )

        except Exception as e:
            raise RuntimeError(
                f"API error from {provider.name} (model: {active_model.name}): {e}"
            ) from e

    async def _chat_streaming(
        self, max_tokens: int | None = None, *, messages: list[LLMMessage] | None = None
    ) -> AsyncGenerator[LLMChunk]:
        active_model = self.config.get_active_model()
        provider = self.config.get_provider_for_model(active_model)
        cache_plan = build_cache_plan(provider, active_model, self.session_id)
        request_messages = messages if messages is not None else self.messages

        send_tools = active_model.capabilities.tools
        available_tools = (
            self.format_handler.get_available_tools(self.tool_manager, self.config)
            if send_tools
            else None
        )
        tool_choice = (
            self.format_handler.get_tool_choice()
            if send_tools and active_model.capabilities.tool_choice
            else None
        )
        try:
            start_time = time.perf_counter()
            last_chunk = None
            self.stats.last_usage_message_index = len(self.messages) - 1
            async with self.backend as backend:
                async for chunk in backend.complete_streaming(
                    model=active_model,
                    messages=request_messages,
                    temperature=active_model.temperature,
                    tools=available_tools,
                    tool_choice=tool_choice,
                    extra_headers={
                        "user-agent": get_user_agent(provider.backend),
                        "x-affinity": self.session_id,
                        **session_headers(provider, self.session_id),
                    },
                    max_tokens=max_tokens,
                    cache_plan=cache_plan,
                    thinking_level=self._thinking_level,
                ):
                    last_chunk = chunk
                    processed_message = (
                        self.format_handler.process_api_response_message(chunk.message)
                    )
                    yield LLMChunk(
                        message=processed_message,
                        usage=chunk.usage,
                        finish_reason=chunk.finish_reason,
                    )

            end_time = time.perf_counter()
            if last_chunk is None:
                raise LLMResponseError("Streamed completion returned no chunks")
            if last_chunk.usage is None:
                raise LLMResponseError(
                    "Usage data missing in final chunk of streamed completion"
                )

            self.stats.last_turn_duration = end_time - start_time
            self.stats.last_turn_prompt_tokens = last_chunk.usage.prompt_tokens
            self.stats.last_turn_completion_tokens = last_chunk.usage.completion_tokens
            self.stats.session_prompt_tokens += last_chunk.usage.prompt_tokens
            self.stats.session_completion_tokens += last_chunk.usage.completion_tokens
            self.stats.cache_read_tokens += last_chunk.usage.cache_read_tokens
            self.stats.cache_write_tokens += last_chunk.usage.cache_write_tokens
            self._last_usage = last_chunk.usage

        except Exception as e:
            raise RuntimeError(
                f"API error from {provider.name} (model: {active_model.name}): {e}"
            ) from e

    def _get_tool_instance(self, tool_name: str) -> BaseTool:
        """Get a tool instance, using plan mode configuration for bash if in plan mode."""
        # In plan mode, use special configuration for bash tool
        if self._mode == AgentMode.PLAN and tool_name == "bash":
            from vibe.core.tools.builtins.bash import BashPlanModeConfig

            # Get the tool class
            tool_class = self.tool_manager._available.get(tool_name)
            if tool_class is None:
                raise Exception(f"Tool '{tool_name}' not found")

            # Create plan mode config
            plan_mode_config = BashPlanModeConfig()
            if self.config.workdir is not None:
                plan_mode_config.workdir = self.config.workdir

            # Return a new instance with plan mode config
            return tool_class.from_config(plan_mode_config)

        # For all other cases, use the normal tool manager. Builtin tools are
        # discovered dynamically, so match by name and duck-type the injector.
        instance = self.tool_manager.get(tool_name)
        if tool_name == "task":
            attach_runner = getattr(instance, "attach_runner", None)
            if callable(attach_runner):
                attach_runner(self._get_or_create_subagent_runner())
        elif tool_name == "run_tools_script":
            from vibe.core.tools.script_sandbox import ToolBridge

            attach_bridge = getattr(instance, "attach_bridge", None)
            if callable(attach_bridge):
                attach_bridge(ToolBridge(self.tool_manager, agent=self))
        return instance

    async def _should_execute_tool(  # noqa: PLR0911
        self, tool: BaseTool, args: dict[str, Any], tool_call_id: str
    ) -> ToolDecision:
        tool_name = tool.get_name()
        args_model, _ = tool._get_tool_args_results()
        validated_args = args_model.model_validate(args)

        # Path allowlists/denylists are hard boundaries: they apply in every
        # mode, including auto-approve and plan mode.
        path_decision = tool.check_path_restrictions(validated_args)
        if path_decision == ToolPermission.NEVER:
            self._audit(tool_name, args, AuditDecision.DENYLIST)
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback=(
                    f"Tool '{tool_name}' blocked by path restrictions "
                    "(path_allowlist/path_denylist)."
                ),
            )

        # Plan mode: only allow read-only tools
        if self._mode == AgentMode.PLAN:
            if tool_name not in PLAN_MODE_ALLOWED_TOOLS:
                self._audit(tool_name, args, AuditDecision.DENIED)
                return ToolDecision(
                    verdict=ToolExecutionResponse.SKIP,
                    feedback=(
                        f"Plan mode: '{tool_name}' is not allowed. "
                        f"Only read-only tools ({', '.join(sorted(PLAN_MODE_ALLOWED_TOOLS))}) are permitted."
                    ),
                )

            # For bash tool in plan mode, check the restrictive allowlist/denylist
            if tool_name == "bash":
                allowlist_denylist_result = tool.check_allowlist_denylist(
                    validated_args
                )
                if allowlist_denylist_result == ToolPermission.NEVER:
                    self._audit(tool_name, args, AuditDecision.DENYLIST)
                    return ToolDecision(
                        verdict=ToolExecutionResponse.SKIP,
                        feedback=(
                            "Plan mode: Command blocked. Only read-only bash commands are allowed. "
                            "Blocked commands include: file modification (touch, mkdir, rm, mv, cp), "
                            "git write operations (commit, push, add), network operations (curl, wget), etc."
                        ),
                    )

            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)

        # Auto-approve mode: execute all tools
        if self._mode == AgentMode.AUTO_APPROVE:
            self._audit(tool_name, args, AuditDecision.APPROVED)
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)

        # Interactive mode: check permissions
        allowlist_denylist_result = tool.check_allowlist_denylist(validated_args)
        if (
            allowlist_denylist_result is ToolPermission.ALWAYS
            or path_decision is ToolPermission.ALWAYS
        ):
            self._audit(tool_name, args, AuditDecision.APPROVED)
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)
        if allowlist_denylist_result is ToolPermission.NEVER:
            self._audit(tool_name, args, AuditDecision.DENYLIST)
            denylist_patterns = tool.config.denylist
            denylist_str = ", ".join(repr(pattern) for pattern in denylist_patterns)
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback=f"Tool '{tool.get_name()}' blocked by denylist: [{denylist_str}]",
            )

        perm = self.tool_manager.get_tool_config(tool_name).permission

        if perm is ToolPermission.ALWAYS:
            self._audit(tool_name, args, AuditDecision.APPROVED)
            return ToolDecision(verdict=ToolExecutionResponse.EXECUTE)
        if perm is ToolPermission.NEVER:
            self._audit(tool_name, args, AuditDecision.DENIED)
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback=f"Tool '{tool_name}' is permanently disabled",
            )

        return await self._ask_approval(tool_name, args, tool_call_id)

    def _audit(
        self, tool_name: str, args: dict[str, Any], decision: AuditDecision
    ) -> None:
        if self.audit_logger is None:
            return
        self.audit_logger.record(
            tool_name, args, decision, mode=self._mode.value, session_id=self.session_id
        )

    def _record_metric(self, name: str, amount: int = 1) -> None:
        if self.metrics is not None:
            self.metrics.increment(name, amount)

    def _log_request_debug(self, request_messages: list[LLMMessage] | None) -> None:
        """Record request metadata only (never message contents)."""
        if self.debug_logger is None:
            return
        try:
            model_alias = self.config.get_active_model().alias
        except ValueError:
            model_alias = self.config.active_model
        self.debug_logger.log_request(
            model=model_alias,
            message_count=len(
                request_messages if request_messages is not None else self.messages
            ),
            estimated_tokens=self.stats.context_tokens,
            duration=self.stats.last_turn_duration,
            tool_count=len(self.tool_manager.available_tools()),
        )

    async def _ask_approval(
        self, tool_name: str, args: dict[str, Any], tool_call_id: str
    ) -> ToolDecision:
        if not self.approval_callback:
            self._audit(tool_name, args, AuditDecision.DENIED)
            return ToolDecision(
                verdict=ToolExecutionResponse.SKIP,
                feedback="Tool execution not permitted.",
            )
        if asyncio.iscoroutinefunction(self.approval_callback):
            response, feedback = await self.approval_callback(
                tool_name, args, tool_call_id
            )
        else:
            sync_callback = cast(SyncApprovalCallback, self.approval_callback)
            response, feedback = sync_callback(tool_name, args, tool_call_id)

        match response:
            case ApprovalResponse.ALWAYS:
                self.auto_approve = True
                self._audit(tool_name, args, AuditDecision.APPROVED)
                return ToolDecision(
                    verdict=ToolExecutionResponse.EXECUTE, feedback=feedback
                )
            case ApprovalResponse.YES:
                self._audit(tool_name, args, AuditDecision.APPROVED)
                return ToolDecision(
                    verdict=ToolExecutionResponse.EXECUTE, feedback=feedback
                )
            case _:
                self._audit(tool_name, args, AuditDecision.DENIED)
                return ToolDecision(
                    verdict=ToolExecutionResponse.SKIP, feedback=feedback
                )

    def _clean_message_history(self) -> None:
        ACCEPTABLE_HISTORY_SIZE = 2
        if len(self.messages) < ACCEPTABLE_HISTORY_SIZE:
            return
        self._answer_pending_tool_calls()
        self._ensure_assistant_after_tools()

    def _answer_pending_tool_calls(self) -> None:
        """Record a placeholder response for calls that were never answered.

        Only the trailing turn is recorded: gaps in older history are repaired
        virtually by the context builder, while the log stays append-only.
        """
        for call in pending_tool_calls(self.messages):
            self._append_message(placeholder_response(call))

    def _ensure_assistant_after_tools(self) -> None:
        MIN_MESSAGE_SIZE = 2
        if len(self.messages) < MIN_MESSAGE_SIZE:
            return

        last_msg = self.messages[-1]
        if last_msg.role is Role.tool:
            empty_assistant_msg = LLMMessage(role=Role.assistant, content="Understood.")
            self._append_message(empty_assistant_msg)

    def set_approval_callback(self, callback: ApprovalCallback) -> None:
        self.approval_callback = callback

    async def clear_history(self) -> None:
        """Clear the conversation the model sees, keeping the session itself.

        The session log, its id, the totals and the cost are preserved; only a
        head marker is recorded, so the context restarts from the system prompt.
        """
        await reset_context(self, reason="clear")

    def _load_conversation(
        self, filepath: Path | None, messages: list[LLMMessage]
    ) -> None:
        """Adopt the entries of a session file, or record ``messages`` as new ones."""
        self._context = None
        self._log = []
        file_backed = (
            filepath is not None
            and filepath.exists()
            and self.interaction_logger.writes_entries
        )
        if file_backed:
            self._log = self._read_session_entries(filepath)
            return
        for message in messages:
            if message.role is not Role.system:
                self._append_message(message)

    async def switch_session(
        self,
        *,
        session_id: str,
        filepath: Path | None,
        messages: list[LLMMessage],
        stats: AgentStats | None = None,
    ) -> None:
        """Point the agent at ``filepath`` and load ``messages`` as its history.

        Used by ``/resume``, ``/fork`` and ``/import``. Subsequent turns append
        to the given session file instead of starting a new one.
        """
        await self._recorder.flush()

        self.session_id = session_id
        self.interaction_logger = InteractionLogger(
            self.config.session_logging,
            session_id,
            self._mode == AgentMode.AUTO_APPROVE,
            self.config.effective_workdir,
            existing_filepath=(
                filepath
                if filepath is not None and self.config.session_logging.enabled
                else None
            ),
        )
        self._recorder = SessionRecorder(self.interaction_logger, agent=self)
        self._load_conversation(filepath, messages)

        if stats is not None:
            self.stats = stats
        try:
            active_model = self.config.get_active_model()
            self.stats.update_pricing(active_model.pricing)
            self.stats.context_window = active_model.context_window
        except ValueError:
            pass

        self._last_usage = None
        self.compactor.reset()
        self.middleware_pipeline.reset()
        self.tool_manager.reset_all()

        if self.message_observer:
            for msg in self.messages:
                self.message_observer(msg)
            self._last_observed_message_index = len(self.messages)

    async def compact(
        self, instructions: str | None = None, *, reason: CompactionReason = "manual"
    ) -> str:
        self._record_metric(COUNTER_COMPACTIONS)
        try:
            result = await self.compactor.compact(
                instructions=instructions, force=True, reason=reason
            )
            return result.summary
        except Exception:
            await self._recorder.flush()
            raise

    def _regenerate_system_prompt_for_mode(self) -> SystemPromptChangedEvent | None:
        """Update only the mode section, keeping the cacheable prefix stable."""
        if self._mode == AgentMode.PLAN:
            self._prompt_sections.set(
                PromptSection(
                    "mode",
                    UtilityPrompt.PLAN_MODE.read(),
                    tagged=False,
                    cache_stable=False,
                )
            )
        else:
            self._prompt_sections.remove("mode")

        self._refresh_tool_sections()
        return self._apply_prompt_sections()

    def _refresh_tool_sections(self) -> None:
        if tool_prompts := collect_tool_prompts(self.tool_manager, self.config):
            self._prompt_sections.set(
                PromptSection("tool_prompts", tool_prompts, tagged=False)
            )
        else:
            self._prompt_sections.remove("tool_prompts")

        if skills := self.skill_manager.get_skills_prompt_section():
            self._prompt_sections.set(PromptSection("skills", skills, tagged=False))
        else:
            self._prompt_sections.remove("skills")

    def _on_mcp_tools_changed(self) -> None:
        if getattr(self, "tool_manager", None) is None:
            return
        if getattr(self, "_prompt_sections", None) is None:
            return
        self._pending_events.append(MCPToolsChangedEvent())
        self._refresh_tool_sections()
        self._apply_prompt_sections()

    def refresh_mcp_tools(self) -> None:
        """Re-sync MCP tool classes and rebuild tool-dependent prompt sections."""
        if self.mcp_manager is None:
            return
        self.tool_manager.apply_mcp_tools()
        self._on_mcp_tools_changed()

    def _apply_prompt_sections(self) -> SystemPromptChangedEvent | None:
        content = self._prompt_sections.render()
        if self._system_message.content == content:
            return None

        self._set_system_message(content)
        return SystemPromptChangedEvent(content=content)

    def add_additional_directory_context(self, directory_path: Path) -> None:
        """Add an additional directory context to the agent."""
        # Resolve the path to absolute form
        resolved_path = directory_path.resolve()

        # Check if this directory is already added
        if resolved_path in self._additional_directory_contexts:
            raise ValueError(f"Directory {resolved_path} is already added to contexts")

        # Add to the list of additional contexts
        self._additional_directory_contexts.append(resolved_path)

    def _regenerate_system_prompt_with_additional_contexts(self) -> None:
        """Regenerate the system prompt with additional directory contexts."""
        if additional := build_additional_contexts(
            self.config, self._additional_directory_contexts
        ):
            self._prompt_sections.set(
                PromptSection("additional_context", additional, tagged=False)
            )
        else:
            self._prompt_sections.remove("additional_context")

        self._apply_prompt_sections()

    async def reload_with_initial_messages(
        self,
        config: VibeConfig | None = None,
        max_turns: int | None = None,
        max_price: float | None = None,
    ) -> None:
        await self._recorder.flush()

        old_system_prompt = self._system_message.content
        previous_model_alias = self._active_model_alias()

        if config is not None:
            self.config = config
            self.backend = self.backend_factory()

        new_model_alias = self._active_model_alias()
        if new_model_alias is not None and new_model_alias != previous_model_alias:
            try:
                active_model = self.config.get_active_model()
            except ValueError:
                pass
            else:
                self._record_model_change(active_model.alias, active_model.provider)

        # Recreate skill manager and tool manager with updated config
        self.skill_manager = SkillManager(self.config)
        self.tool_manager = ToolManager(
            self.config,
            skill_manager=self.skill_manager,
            mcp_manager=self.mcp_manager,
            tools_changed_callback=self._on_mcp_tools_changed,
        )
        self.hooks = load_hooks(self.config)

        self._prompt_sections = build_prompt_sections(
            self.tool_manager,
            self.config,
            skill_manager=self.skill_manager,
            mode=self._mode.value,
            additional_directory_contexts=self._additional_directory_contexts,
        )
        new_system_prompt = self._prompt_sections.render()
        did_system_prompt_change = old_system_prompt != new_system_prompt
        self._set_system_message(new_system_prompt)

        if len(self.messages) == 1 or did_system_prompt_change:
            self.stats.reset_context_state()
            self._last_usage = None

        try:
            active_model = self.config.get_active_model()
            self.stats.update_pricing(active_model.pricing)
            self.stats.context_window = active_model.context_window
        except ValueError:
            pass

        self._last_observed_message_index = 0

        self._setup_middleware(max_turns, max_price)

        if self.message_observer:
            for msg in self.messages:
                self.message_observer(msg)
            self._last_observed_message_index = len(self.messages)

        self.tool_manager.reset_all()

        await self._recorder.flush()

    def _active_model_alias(self) -> str | None:
        try:
            return self.config.get_active_model().alias
        except ValueError:
            return None


def edit_context(
    agent: Agent,
    target_id: str,
    action: EditAction = "omit",
    replacement: str | None = None,
) -> ContextEditEntry:
    """Record an omit or replace edit for ``target_id`` without rewriting the log."""
    entry = ContextEditEntry(
        id=new_entry_id(),
        parent_id=agent._last_entry_id(),
        timestamp=now_iso(),
        target_id=target_id,
        action=action,
        replacement=replacement,
    )
    agent._append_entry(entry)
    agent._refresh_context_stats()
    return entry


async def reset_context(
    agent: Agent, *, keep_from_id: str | None = None, reason: str = "reset"
) -> HeadMarkerEntry:
    """Record a head marker so the context restarts while keeping the session log."""
    await agent._recorder.flush()
    marker = HeadMarkerEntry(
        id=new_entry_id(),
        parent_id=agent._last_entry_id(),
        timestamp=now_iso(),
        keep_from_id=keep_from_id,
        reason=reason,
    )
    agent._append_entry(marker)
    agent._last_usage = None
    agent.stats.reset_context_state()
    agent.compactor.reset()
    agent.middleware_pipeline.reset()
    agent.tool_manager.reset_all()
    agent._refresh_context_stats()
    await agent._recorder.flush()
    return marker


def context_overview(agent: Agent, *, recent: int = 10) -> ContextOverview:
    """Summarize the context and session log behind ``agent`` for ``/context``."""
    context = agent._context_result()
    head_marker = next(
        (entry for entry in reversed(agent._log) if isinstance(entry, HeadMarkerEntry)),
        None,
    )
    actions = [edit.action for edit in context.edits.values()]
    return ContextOverview(
        messages=len(context.messages) - 1,
        entries=len(agent._log),
        head_marker=head_marker,
        omitted=actions.count("omit"),
        replaced=actions.count("replace"),
        recent_entries=agent._log[-recent:],
    )
