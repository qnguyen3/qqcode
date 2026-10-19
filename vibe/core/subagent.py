from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Literal

from vibe.core.agent import Agent
from vibe.core.config import SubagentConfig, VibeConfig
from vibe.core.tools.base import BaseToolConfig
from vibe.core.tools.builtins.task import TaskArgs, TaskResult
from vibe.core.types import (
    AgentMode,
    AssistantEvent,
    SubagentEvent,
    SubagentProgressEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from vibe.core.utils import TaggedText

logger = logging.getLogger("vibe")

DEFAULT_MAX_TURNS = 15
READ_ONLY_TOOLS = ("read_file", "grep", "find", "ls")
FORBIDDEN_CHILD_TOOLS = ("task", "skill")
_PATH_GUARDED_TOOLS = ("read_file", "write_file", "edit", "grep", "find", "ls")


class SubAgentRunner:
    """Runs delegated work as a child agent with isolated context and budgets.

    The runner is owned by the parent :class:`~vibe.core.agent.Agent` and shared
    by every ``task`` tool instance. It enforces concurrency, timeout and the
    child toolset restrictions, and always flushes the child session.
    """

    def __init__(self, parent: Agent) -> None:
        self._parent = parent
        self._lock = asyncio.Lock()
        self._active = 0

    @property
    def active_count(self) -> int:
        """Number of subagent runs currently executing."""
        return self._active

    def _settings(self) -> SubagentConfig:
        return self._parent.config.subagent

    def _emit(self, event: SubagentProgressEvent) -> None:
        self._parent._emit_subagent_event(event)

    async def run(self, args: TaskArgs) -> TaskResult:
        """Run one delegated task to completion and return its result.

        Never raises for expected subagent failures (timeout, budget,
        concurrency, config errors): those are reported in
        :class:`TaskResult`. Parent cancellation always cancels the child.
        """
        settings = self._settings()
        if not await self._acquire(settings.max_concurrent):
            return self._failure(args, "too many concurrent subagents")

        child: Agent | None = None
        run_task: asyncio.Task[TaskResult] | None = None
        try:
            try:
                child = self._create_child(args, settings)
            except Exception as exc:
                return self._failure(args, f"failed to start subagent: {exc}")

            self._emit(
                SubagentEvent(
                    description=args.description,
                    status="running",
                    message=f"subagent '{args.description}' started",
                )
            )
            run_task = asyncio.create_task(self._execute_child(child, args))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(run_task), timeout=settings.timeout
                )
            except TimeoutError:
                return self._failure(
                    args,
                    f"subagent timed out after {settings.timeout:g}s",
                    status="timeout",
                )
            except asyncio.CancelledError:
                self._emit(
                    SubagentEvent(
                        description=args.description,
                        status="cancelled",
                        message="subagent cancelled",
                    )
                )
                raise
            except Exception as exc:
                return self._failure(args, f"subagent failed: {exc}")
        finally:
            try:
                if run_task is not None and not run_task.done():
                    run_task.cancel()
                if run_task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await run_task
                if child is not None:
                    await self._flush(child)
            finally:
                await self._release()

    async def _acquire(self, limit: int) -> bool:
        async with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    async def _release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    def _failure(
        self,
        args: TaskArgs,
        error: str,
        *,
        status: Literal["failed", "timeout"] = "failed",
    ) -> TaskResult:
        self._emit(
            SubagentEvent(description=args.description, status=status, message=error)
        )
        return TaskResult(
            description=args.description,
            output="",
            turns=0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=error,
        )

    def _create_child(self, args: TaskArgs, settings: SubagentConfig) -> Agent:
        config = self._build_child_config(args, settings)
        max_turns = args.max_turns if args.max_turns is not None else DEFAULT_MAX_TURNS
        return Agent(
            config,
            mode=AgentMode.AUTO_APPROVE,
            max_turns=max_turns,
            max_price=settings.max_price,
            backend=self._parent.backend,
            enable_streaming=self._parent.enable_streaming,
            parent_session_id=self._parent.session_id,
        )

    def _build_child_config(
        self, args: TaskArgs, settings: SubagentConfig
    ) -> VibeConfig:
        """Resolve the child config: agent TOML (or parent copy) + overrides."""
        base = self._parent.config
        requested = args.agent or settings.default_agent
        config = self._load_agent_config(
            requested, base, allow_fallback=args.agent is None
        )
        config.workdir = base.effective_workdir
        config.session_logging = base.session_logging
        config.subagent = settings.model_copy(update={"enabled": False})
        self._restrict_tools(config)
        return config

    def _load_agent_config(
        self, name: str, base: VibeConfig, *, allow_fallback: bool
    ) -> VibeConfig:
        if not name:
            return base.model_copy(deep=True)
        try:
            return VibeConfig.load(name, workdir=base.effective_workdir)
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.info(
                "Agent config '%s' is not available (%s); using a restricted "
                "copy of the parent config",
                name,
                exc,
            )
            return base.model_copy(deep=True)

    def _restrict_tools(self, config: VibeConfig) -> None:
        if not config.enabled_tools and not config.disabled_tools:
            config.enabled_tools = list(READ_ONLY_TOOLS)
        elif config.enabled_tools:
            allowed = [
                name
                for name in config.enabled_tools
                if name not in FORBIDDEN_CHILD_TOOLS
            ]
            config.enabled_tools = allowed or list(READ_ONLY_TOOLS)

        config.disabled_tools = list(
            dict.fromkeys([*config.disabled_tools, *FORBIDDEN_CHILD_TOOLS])
        )

        sandbox = [str(config.effective_workdir), str(config.effective_workdir / "**")]
        for name in _PATH_GUARDED_TOOLS:
            if (existing := config.tools.get(name)) is not None:
                existing.path_allowlist = sandbox
            else:
                config.tools[name] = BaseToolConfig(path_allowlist=sandbox)

    async def _execute_child(self, child: Agent, args: TaskArgs) -> TaskResult:
        settings = self._settings()
        final_text = ""
        stop_reason: str | None = None
        emitted_turns = -1

        async with contextlib.aclosing(child.act(args.prompt)) as stream:
            async for event in stream:
                if (turns := max(0, child.stats.steps - 1)) != emitted_turns:
                    emitted_turns = turns
                    self._emit(
                        SubagentEvent(
                            description=args.description,
                            status="running",
                            turns=turns,
                            prompt_tokens=child.stats.session_prompt_tokens,
                            completion_tokens=child.stats.session_completion_tokens,
                        )
                    )

                match event:
                    case AssistantEvent():
                        if event.stopped_by_middleware:
                            stop_reason = TaggedText.from_string(event.content).message
                        elif event.content:
                            final_text = event.content
                    case ToolCallEvent() | ToolResultEvent():
                        if settings.stream_tools:
                            self._emit(self._forward(event))
                    case _:
                        pass

        turns = max(0, child.stats.steps - 1)
        result = TaskResult(
            description=args.description,
            output=final_text,
            turns=turns,
            prompt_tokens=child.stats.session_prompt_tokens,
            completion_tokens=child.stats.session_completion_tokens,
            success=stop_reason is None,
            error=f"subagent stopped early: {stop_reason}" if stop_reason else None,
        )
        self._record_result(child, result)
        self._emit(
            SubagentEvent(
                description=args.description,
                status="completed" if result.success else "failed",
                turns=result.turns,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                message=result.error,
            )
        )
        return result

    def _forward(self, event: ToolCallEvent | ToolResultEvent) -> SubagentProgressEvent:
        return event.model_copy(update={"tool_name": f"[sub] {event.tool_name}"})

    def _record_result(self, child: Agent, result: TaskResult) -> None:
        payload = {
            "description": result.description,
            "session_id": child.session_id,
            "output": result.output,
            "turns": result.turns,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "success": result.success,
            "error": result.error,
        }
        with contextlib.suppress(Exception):
            child.record_custom("subagent_result", payload)
        with contextlib.suppress(Exception):
            self._parent.record_custom(
                "subagent_result", {**payload, "child_session_id": child.session_id}
            )

    async def _flush(self, child: Agent) -> None:
        try:
            await child._recorder.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to flush subagent session %s: %s", child.session_id, exc
            )
