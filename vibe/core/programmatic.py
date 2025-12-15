from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import sys
from typing import Any, Literal

from vibe.core.agent import Agent
from vibe.core.config import VibeConfig
from vibe.core.output_formatters import VScodeJsonFormatter, create_formatter
from vibe.core.types import (
    AgentMode,
    AssistantEvent,
    AsyncApprovalCallback,
    LLMMessage,
    OutputFormat,
    Role,
    StreamEventType,
)
from vibe.core.utils import ConversationLimitException, logger

type ExecutionMode = Literal["plan", "interactive", "auto-approve"]


def _mode_to_agent_mode(mode: ExecutionMode) -> AgentMode:
    """Convert execution mode string to AgentMode enum."""
    match mode:
        case "plan":
            return AgentMode.PLAN
        case "interactive":
            return AgentMode.INTERACTIVE
        case "auto-approve":
            return AgentMode.AUTO_APPROVE


def _create_stdin_approval_callback(
    formatter: VScodeJsonFormatter,
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
        # Emit approval_required event to stdout
        formatter._emit_event(
            StreamEventType.TOOL_APPROVAL_REQUIRED,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args": args,
            },
        )

        # Read response from stdin (blocking, run in executor to avoid blocking event loop)
        loop = asyncio.get_event_loop()
        response_line = await loop.run_in_executor(None, sys.stdin.readline)
        response_line = response_line.strip()

        if not response_line:
            # Empty response = rejection (e.g., stdin closed)
            return ("n", "No response received from stdin")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError:
            return ("n", f"Invalid JSON response: {response_line}")

        # Parse response
        approved = response.get("approved", False)
        reason = response.get("reason")

        if approved:
            return ("y", None)
        else:
            return ("n", reason or "User rejected the tool call")

    return approval_callback


def _create_stdin_plan_approval_callback(
    formatter: VScodeJsonFormatter,
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
        # Emit plan_approval_required event to stdout
        formatter._emit_event(
            StreamEventType.PLAN_APPROVAL_REQUIRED,
            {
                "plan": plan,
            },
        )

        # Read response from stdin (blocking, run in executor to avoid blocking event loop)
        loop = asyncio.get_event_loop()
        response_line = await loop.run_in_executor(None, sys.stdin.readline)
        response_line = response_line.strip()

        if not response_line:
            # Empty response = rejection (e.g., stdin closed)
            return (False, "No response received from stdin")

        try:
            response = json.loads(response_line)
        except json.JSONDecodeError:
            return (False, f"Invalid JSON response: {response_line}")

        # Parse response
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


def run_programmatic(
    config: VibeConfig,
    prompt: str,
    max_turns: int | None = None,
    max_price: float | None = None,
    output_format: OutputFormat = OutputFormat.TEXT,
    previous_messages: list[LLMMessage] | None = None,
    mode: ExecutionMode = "plan",
    session_id: str | None = None,
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

    Returns:
        The final assistant response text, or None if no response
    """
    agent_mode = _mode_to_agent_mode(mode)

    # Create agent with session_id if resuming, otherwise a new session_id is generated
    agent = Agent(
        config,
        mode=agent_mode,
        message_observer=None,  # Will set observer after creating formatter
        max_turns=max_turns,
        max_price=max_price,
        enable_streaming=False,
        session_id=session_id,
    )

    # Create formatter with agent's session_id for VSCode output format
    formatter = create_formatter(output_format, session_id=agent.session_id)

    # Always set up approval callbacks for VSCode output format
    # The agent's mode determines whether they're used, but they must be available
    # for mode transitions (e.g., plan -> interactive after plan approval)
    if isinstance(formatter, VScodeJsonFormatter):
        agent.set_approval_callback(_create_stdin_approval_callback(formatter))
        agent.plan_approval_callback = _create_stdin_plan_approval_callback(formatter)

    # Now set the message observer
    agent.message_observer = formatter.on_message_added
    # Flush existing messages to the observer
    for msg in agent.messages:
        formatter.on_message_added(msg)
    agent._last_observed_message_index = len(agent.messages)

    logger.info("USER: %s", prompt)

    async def _async_run() -> str | None:
        if previous_messages:
            non_system_messages = [
                msg for msg in previous_messages if not (msg.role == Role.system)
            ]
            agent.messages.extend(non_system_messages)
            logger.info(
                "Loaded %d messages from previous session", len(non_system_messages)
            )

        async for event in agent.act(prompt):
            formatter.on_event(event)
            if isinstance(event, AssistantEvent) and event.stopped_by_middleware:
                raise ConversationLimitException(event.content)

        return formatter.finalize()

    return asyncio.run(_async_run())
