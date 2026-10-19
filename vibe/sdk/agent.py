"""Async session API for embedding the QQcode agent in Python applications."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from types import TracebackType

from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, ThinkingLevel, VibeConfig
from vibe.core.events import AgentEventTranslator, EventBus, SDKEvent
from vibe.core.images import describe_image, is_image_path
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.llm.types import BackendLike
from vibe.core.mcp.manager import MCPManager
from vibe.core.sessions.store import (
    JSONL_SUFFIX,
    JsonlSessionStore,
    SessionSummary,
    SessionView,
)
from vibe.core.types import (
    AgentMode,
    AgentStats,
    AssistantEvent,
    BaseEvent,
    CompactEndEvent,
    LLMMessage,
    Role,
)
from vibe.sdk.options import AgentOptions, ExecutionModeName, SessionRef


def _resolve_agent_mode(
    mode: ExecutionModeName | None, *, auto_approve: bool
) -> AgentMode:
    if mode is not None:
        match mode:
            case "plan":
                return AgentMode.PLAN
            case "interactive":
                return AgentMode.INTERACTIVE
            case "auto-approve":
                return AgentMode.AUTO_APPROVE
    return AgentMode.AUTO_APPROVE if auto_approve else AgentMode.INTERACTIVE


def _parse_thinking_level(value: str | None) -> ThinkingLevel | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"default", "none", "auto", ""}:
        return None
    try:
        return ThinkingLevel(normalized)
    except ValueError as exc:
        valid = ", ".join(level.value for level in ThinkingLevel)
        raise ValueError(
            f"Invalid thinking level '{value}'. Valid levels: {valid}"
        ) from exc


def _build_config(options: AgentOptions) -> VibeConfig:
    if options.config is not None:
        config = options.config.model_copy(deep=True)
    else:
        config = VibeConfig.load(config_path=options.config_path, agent=options.agent)

    if options.model_alias is not None:
        config.active_model = options.model_alias
    if options.enabled_tools is not None:
        config.enabled_tools = list(options.enabled_tools)
    if options.system_prompt is not None:
        config.system_prompt_override = options.system_prompt
    if options.append_system_prompt:
        config.append_system_prompt = [
            *config.append_system_prompt,
            *options.append_system_prompt,
        ]
    if "working_dir" in options.model_fields_set or config.workdir is None:
        config.workdir = options.working_dir
    if options.session_dir is not None:
        config.session_logging.sessions_dir = str(options.session_dir)
    if options.config is None or "save_session" in options.model_fields_set:
        config.session_logging.enabled = options.save_session

    return config


def _resolve_session_ref(
    ref: SessionRef | None, config: VibeConfig
) -> tuple[str | None, str | None, SessionView | None]:
    if ref is None:
        return None, None, None

    store = JsonlSessionStore(config.session_logging)
    source_path: Path | None = None
    if ref.session_id is not None:
        source_path = InteractionLogger.find_session_by_id(
            ref.session_id, config.session_logging
        )
    elif ref.fork_at_entry_id is not None:
        source_path = InteractionLogger.find_latest_session(config.session_logging)

    if ref.fork_at_entry_id is not None:
        if source_path is None:
            raise FileNotFoundError(
                f"Cannot fork session '{ref.session_id or 'latest'}': session file not found"
            )
        forked_path = store.fork(
            source_path, ref.fork_at_entry_id, workdir=config.effective_workdir
        )
        view = store.read(forked_path)
        return view.header.id, view.header.parent_session_id, view

    if source_path is not None and source_path.suffix == JSONL_SUFFIX:
        view = store.read(source_path)
        return view.header.id, view.header.parent_session_id, view

    return ref.session_id, None, None


def _restore_stats_from_view(agent: Agent, view: SessionView) -> None:
    restored = view.stats()
    try:
        active_model = agent.config.get_active_model()
        restored.update_pricing(active_model.pricing)
        restored.context_window = active_model.context_window
    except ValueError:
        pass
    agent.stats = restored
    agent._refresh_context_stats()


class AgentSession:
    """An embedded, async-controllable QQcode agent session."""

    def __init__(
        self, options: AgentOptions | None = None, *, backend: BackendLike | None = None
    ) -> None:
        self._options = options or AgentOptions()
        config = _build_config(self._options)
        mode = _resolve_agent_mode(
            self._options.mode, auto_approve=self._options.auto_approve
        )
        session_id, parent_session_id, view = _resolve_session_ref(
            self._options.session, config
        )
        resolved_backend = backend or self._options.backend
        self._mcp_manager = MCPManager(config)
        self._agent = Agent(
            config,
            mode=mode,
            max_turns=self._options.max_turns,
            max_price=self._options.max_price,
            backend=resolved_backend,
            enable_streaming=self._options.enable_streaming,
            session_id=session_id,
            parent_session_id=parent_session_id,
            mcp_manager=self._mcp_manager,
        )
        if view is not None:
            _restore_stats_from_view(self._agent, view)

        self.bus = EventBus()
        self._translator = AgentEventTranslator(self.bus, self._agent.session_id)
        self._session_started = False
        self._mcp_started = False
        self._closed = False
        self._last_stopped_by_middleware: AssistantEvent | None = None

    @property
    def agent(self) -> Agent:
        """Underlying :class:`~vibe.core.agent.Agent` instance."""
        return self._agent

    @property
    def session_id(self) -> str:
        return self._agent.session_id

    @property
    def last_stopped_by_middleware(self) -> AssistantEvent | None:
        return self._last_stopped_by_middleware

    def stats(self) -> AgentStats:
        return self._agent.stats.model_copy(deep=True)

    def messages(self) -> list[LLMMessage]:
        return list(self._agent.messages)

    async def __aenter__(self) -> AgentSession:
        await self._ensure_mcp_started()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _ensure_mcp_started(self) -> None:
        if self._mcp_started:
            return
        self._mcp_started = True
        await self._mcp_manager.start()
        self._agent.refresh_mcp_tools()

    def _attach_files(self, attachments: Sequence[Path]) -> None:
        for raw_path in attachments:
            path = Path(raw_path).expanduser().resolve()
            if is_image_path(path):
                part = describe_image(path)
                self._agent.add_message(
                    LLMMessage(
                        role=Role.user,
                        content=f"Attached image: {path.name}",
                        images=[part],
                    )
                )
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                self._agent.add_message(
                    LLMMessage(
                        role=Role.user,
                        content=f'<attachment path="{path.name}">\n{text}\n</attachment>',
                    )
                )

    async def prompt(
        self,
        text: str,
        *,
        attachments: Sequence[Path] = (),
        on_raw_event: Callable[[BaseEvent], None] | None = None,
    ) -> AsyncIterator[SDKEvent]:
        """Run a prompt turn and stream :data:`~vibe.sdk.events.SDKEvent` items."""
        await self._ensure_mcp_started()
        if attachments:
            self._attach_files(attachments)

        self._last_stopped_by_middleware = None
        queue, unsubscribe = self.bus.subscribe_queue()

        if not self._session_started:
            self._session_started = True
            self._translator.start_session()

        async def _drive() -> None:
            try:
                async for raw_event in self._agent.act(text):
                    if callable(on_raw_event):
                        on_raw_event(raw_event)
                    self._translator.on_event(raw_event)
                    if (
                        isinstance(raw_event, AssistantEvent)
                        and raw_event.stopped_by_middleware
                    ):
                        self._last_stopped_by_middleware = raw_event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._translator.emit_error(str(exc))
                raise
            finally:
                queue.put_nowait(None)

        driver = asyncio.create_task(_drive(), name="sdk-prompt")
        try:
            while (event := await queue.get()) is not None:
                yield event
            await driver
        finally:
            unsubscribe()
            if not driver.done():
                driver.cancel()
                with suppress(asyncio.CancelledError):
                    await driver

    async def steer(self, text: str) -> None:
        """Queue a steering user message to be injected after the active turn."""
        if not text.strip():
            raise ValueError("steer requires a non-empty message")
        self._agent.steer(text)

    async def abort(self) -> None:
        """Abort the currently executing turn, if any."""
        self._agent.abort()

    async def compact(self, instructions: str | None = None) -> str:
        """Compact conversation history and publish a ``session.compacted`` event."""
        before = self._agent.stats.context_tokens
        summary = await self._agent.compact(instructions=instructions)
        self._translator.on_event(
            CompactEndEvent(
                old_context_tokens=before,
                new_context_tokens=self._agent.stats.context_tokens,
                summary_length=len(summary),
            )
        )
        return summary

    async def set_model(self, alias: str) -> None:
        """Switch the active model by alias."""
        if not alias.strip():
            raise ValueError("set_model requires a non-empty model alias")
        self._agent.set_model(alias)

    async def set_thinking(self, level: str | None) -> None:
        """Set the reasoning/thinking effort level for subsequent turns."""
        parsed = _parse_thinking_level(level)
        self._agent.set_thinking_level(parsed, record=True)

    def on_raw_event(self, event: BaseEvent) -> None:
        """Forward an internal agent event onto the session's ``EventBus``."""
        self._translator.on_event(event)

    async def close(self) -> None:
        """Flush session state and stop background MCP connections."""
        if self._closed:
            return
        self._closed = True
        await self._agent._recorder.flush()
        if self._mcp_started:
            await self._mcp_manager.stop()
            self._mcp_started = False

    @staticmethod
    def list_sessions(
        workdir: Path | None = None,
        *,
        limit: int = 20,
        sessions_dir: Path | None = None,
    ) -> list[SessionSummary]:
        """List saved JSONL sessions from the session store."""
        config = (
            SessionLoggingConfig(sessions_dir=str(sessions_dir))
            if sessions_dir is not None
            else None
        )
        store = JsonlSessionStore(config)
        return store.list(workdir=workdir, limit=limit)


def create_agent(
    options: AgentOptions | None = None, *, backend: BackendLike | None = None
) -> AgentSession:
    """Create a new :class:`AgentSession` from ``options``."""
    return AgentSession(options, backend=backend)
