from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import json
import sys
import threading
from typing import Any, Literal, Protocol, TextIO

from vibe.core.agent import Agent
from vibe.core.config import ThinkingLevel, VibeConfig
from vibe.core.output_formatters import (
    EventJsonlFormatter,
    VScodeJsonFormatter,
    create_formatter,
)
from vibe.core.types import (
    AgentMode,
    AssistantEvent,
    AsyncApprovalCallback,
    CompactEndEvent,
    LLMMessage,
    OutputFormat,
    Role,
    StreamEventType,
)
from vibe.core.utils import ConversationLimitException, logger

type ExecutionMode = Literal["plan", "interactive", "auto-approve"]


class EventEmitter(Protocol):
    def _emit_event(
        self, event_type: StreamEventType, data: dict[str, Any]
    ) -> None: ...


def _mode_to_agent_mode(mode: ExecutionMode) -> AgentMode:
    """Convert execution mode string to AgentMode enum."""
    match mode:
        case "plan":
            return AgentMode.PLAN
        case "interactive":
            return AgentMode.INTERACTIVE
        case "auto-approve":
            return AgentMode.AUTO_APPROVE


class StdinChannel:
    """Read stdin on a daemon thread and route lines to the right consumer.

    JSON objects carrying a ``type`` field become control commands; every other
    line is treated as a response for a pending approval callback. A dedicated
    thread avoids blocking the event loop and lets the process exit even while
    a read is pending.
    """

    def __init__(self, stream: TextIO = sys.stdin) -> None:
        self._stream = stream
        self._controls: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._read_lines, name="stdin-reader", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._loop = None

    def _read_lines(self) -> None:
        try:
            while line := self._stream.readline():
                text = line.strip()
                if not text:
                    continue
                if (payload := _parse_control_line(text)) is not None:
                    self._deliver(self._controls, payload)
                else:
                    self._deliver(self._responses, text)
        except Exception as exc:
            logger.debug("stdin reader stopped: %s", exc)
        finally:
            self._deliver(self._controls, None)

    def _deliver(self, queue: asyncio.Queue, item: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    async def next_control(self) -> dict[str, Any] | None:
        return await self._controls.get()

    async def next_response(self) -> str:
        return await self._responses.get()


def _parse_control_line(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload
    return None


def _parse_thinking_level(value: Any) -> ThinkingLevel | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Thinking level must be a string")
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


class ControlCommandError(ValueError):
    """Raised when a stdin control command is malformed or unknown."""


async def dispatch_control_command(
    agent: Agent, command: dict[str, Any], formatter: EventJsonlFormatter
) -> bool:
    """Apply one stdin control command to ``agent``.

    Returns ``True`` when the command was accepted.
    """
    try:
        await _apply_control_command(agent, command, formatter)
    except ControlCommandError as exc:
        formatter.emit_error(str(exc))
        return False
    return True


async def _apply_control_command(
    agent: Agent, command: dict[str, Any], formatter: EventJsonlFormatter
) -> None:
    match command.get("type"):
        case "compact":
            instructions = command.get("instructions")
            before = agent.stats.context_tokens
            try:
                summary = await agent.compact(
                    instructions=instructions if isinstance(instructions, str) else None
                )
            except Exception as exc:
                raise ControlCommandError(f"Compaction failed: {exc}") from exc
            formatter.on_event(
                CompactEndEvent(
                    old_context_tokens=before,
                    new_context_tokens=agent.stats.context_tokens,
                    summary_length=len(summary),
                )
            )

        case "abort":
            agent.abort()

        case "set_model":
            alias = command.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                raise ControlCommandError("set_model requires an 'alias' string")
            try:
                agent.set_model(alias)
            except ValueError as exc:
                raise ControlCommandError(str(exc)) from exc

        case "set_thinking":
            try:
                level = _parse_thinking_level(command.get("level"))
            except ValueError as exc:
                raise ControlCommandError(str(exc)) from exc
            agent.set_thinking_level(level, record=True)

        case "steer":
            message = command.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ControlCommandError("steer requires a non-empty 'message'")
            agent.steer(message)

        case other:
            raise ControlCommandError(f"Unknown control command: {other}")


async def consume_control_commands(
    agent: Agent, formatter: EventJsonlFormatter, channel: StdinChannel
) -> None:
    while (command := await channel.next_control()) is not None:
        await dispatch_control_command(agent, command, formatter)


def _create_stdin_approval_callback(
    formatter: EventEmitter, channel: StdinChannel | None = None
) -> AsyncApprovalCallback:
    """Create an approval callback that uses stdin/stdout for communication.

    This callback:
    1. Emits a tool.approval_required event to stdout
    2. Waits for a JSON response on stdin
    3. Returns the approval decision to the agent

    Expected stdin format:
    {"tool_call_id": "...", "approved": true/false, "reason": "optional"}
    """

    async def approval_callback(
        tool_name: str, args: dict[str, Any], tool_call_id: str
    ) -> tuple[str, str | None]:
        formatter._emit_event(
            StreamEventType.TOOL_APPROVAL_REQUIRED,
            {"tool_call_id": tool_call_id, "tool_name": tool_name, "args": args},
        )

        response_line = await _read_response_line(channel)

        if not response_line:
            # Empty response = rejection (e.g., stdin closed)
            return ("n", "No response received from stdin")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError:
            return ("n", f"Invalid JSON response: {response_line}")

        approved = response.get("approved", False)
        reason = response.get("reason")

        if approved:
            return ("y", None)
        else:
            return ("n", reason or "User rejected the tool call")

    return approval_callback


def _create_stdin_plan_approval_callback(
    formatter: EventEmitter, channel: StdinChannel | None = None
) -> Callable[[str], Awaitable[tuple[bool, str | None]]]:
    """Create a plan approval callback that uses stdin/stdout for communication.

    This callback:
    1. Emits a plan.approval_required event to stdout
    2. Waits for a JSON response on stdin
    3. Returns the approval decision to the agent

    Expected stdin format:
    {"approved": true/false, "mode": "auto-approve"/"interactive", "feedback": "optional"}
    """

    async def plan_approval_callback(plan: str) -> tuple[bool, str | None]:
        formatter._emit_event(StreamEventType.PLAN_APPROVAL_REQUIRED, {"plan": plan})

        response_line = await _read_response_line(channel)

        if not response_line:
            # Empty response = rejection (e.g., stdin closed)
            return (False, "No response received from stdin")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError:
            return (False, f"Invalid JSON response: {response_line}")

        approved = response.get("approved", False)
        mode = response.get("mode")
        feedback = response.get("feedback")

        if approved:
            # Convert mode string to AgentMode if provided
            if mode:
                return (True, mode)
            else:
                return (True, None)
        else:
            return (False, feedback or "User rejected the plan")

    return plan_approval_callback


async def _read_response_line(channel: StdinChannel | None) -> str:
    if channel is not None:
        return (await channel.next_response()).strip()
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    return line.strip()


def run_programmatic(
    config: VibeConfig,
    prompt: str,
    max_turns: int | None = None,
    max_price: float | None = None,
    output_format: OutputFormat = OutputFormat.TEXT,
    previous_messages: list[LLMMessage] | None = None,
    mode: ExecutionMode = "plan",
    session_id: str | None = None,
    session_name: str | None = None,
) -> str | None:
    """Run in programmatic mode: execute prompt and return the assistant response.

    Args:
        config: Configuration for the Vibe agent
        prompt: The user prompt to process
        max_turns: Maximum number of assistant turns (LLM calls) to allow
        max_price: Maximum cost in dollars before stopping
        output_format: Format for the output
        previous_messages: Optional messages from a previous session to continue
        mode: Execution mode - 'plan' (read-only), 'interactive' (approval via stdin),
              or 'auto-approve' (execute all tools)
        session_id: Optional session ID to preserve when resuming a session
        session_name: Optional human readable name stored as a custom entry

    Returns:
        The final assistant response text, or None if no response
    """
    from vibe.sdk.agent import AgentSession
    from vibe.sdk.options import AgentOptions, SessionRef

    session = AgentSession(
        AgentOptions(
            config=config,
            mode=mode,
            max_turns=max_turns,
            max_price=max_price,
            enable_streaming=False,
            session=SessionRef(session_id=session_id) if session_id else None,
        )
    )
    agent = session.agent

    # Create formatter with agent's session_id for structured output formats
    formatter = create_formatter(output_format, session_id=agent.session_id)

    # Always set up approval callbacks for structured streaming output formats.
    # The agent's mode determines whether they're used, but they must be available
    # for mode transitions (e.g., plan -> interactive after plan approval).
    channel: StdinChannel | None = None
    events_formatter = formatter if isinstance(formatter, EventJsonlFormatter) else None
    if events_formatter is not None:
        channel = StdinChannel()
        agent.set_approval_callback(
            _create_stdin_approval_callback(events_formatter, channel)
        )
        agent.plan_approval_callback = _create_stdin_plan_approval_callback(
            events_formatter, channel
        )
    elif isinstance(formatter, VScodeJsonFormatter):
        agent.set_approval_callback(_create_stdin_approval_callback(formatter))
        agent.plan_approval_callback = _create_stdin_plan_approval_callback(formatter)

    # Now set the message observer
    agent.message_observer = formatter.on_message_added
    # Flush existing messages to the observer
    for msg in agent.messages:
        formatter.on_message_added(msg)
    agent._last_observed_message_index = len(agent.messages)

    if session_name:
        agent.record_custom("name", {"name": session_name})

    logger.info("USER: %s", prompt)

    async def _run_agent() -> str | None:
        # A resumed session already replays its own log, so only a fresh agent
        # adopts the previous messages.
        if previous_messages and len(agent.messages) == 1:
            non_system_messages = [
                msg for msg in previous_messages if not (msg.role == Role.system)
            ]
            for message in non_system_messages:
                agent.add_message(message)
            logger.info(
                "Loaded %d messages from previous session", len(non_system_messages)
            )

        async for event in agent.act(prompt):
            formatter.on_event(event)
            if isinstance(event, AssistantEvent) and event.stopped_by_middleware:
                raise ConversationLimitException(event.content)

        return formatter.finalize()

    async def _async_run() -> str | None:
        control_task: asyncio.Task[None] | None = None
        if channel is not None and events_formatter is not None:
            channel.start()
            control_task = asyncio.create_task(
                consume_control_commands(agent, events_formatter, channel),
                name="stdin-control",
            )

        try:
            async with session:
                return await _run_agent_with_mcp(agent, _run_agent)
        except asyncio.CancelledError:
            logger.info("Agent turn aborted")
            return formatter.finalize()
        except Exception as exc:
            if events_formatter is not None:
                events_formatter.emit_error(str(exc))
            raise
        finally:
            if control_task is not None:
                control_task.cancel()
            if channel is not None:
                channel.close()

    return asyncio.run(_async_run())


async def _run_agent_with_mcp(
    agent: Agent, run: Callable[[], Coroutine[Any, Any, str | None]]
) -> str | None:
    manager = agent.mcp_manager
    if manager is not None:
        await manager.start()
        agent.refresh_mcp_tools()
    task = asyncio.create_task(run(), name="agent-run")
    try:
        return await task
    finally:
        task.cancel()
        if manager is not None:
            await manager.stop()
