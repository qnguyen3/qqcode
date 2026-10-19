from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from vibe.core.agent import Agent
from vibe.core.config import ThinkingLevel
from vibe.core.context.compactor import CompactionReason
from vibe.core.output_formatters import EventJsonlFormatter
from vibe.core.programmatic import (
    StdinChannel,
    consume_control_commands,
    dispatch_control_command,
)


class ControlAgent(Agent):
    def __init__(self) -> None:
        self.stats = SimpleNamespace(context_tokens=10)
        self.steered: list[str] = []
        self.abort_called = False
        self.switched_model: str | None = None
        self.thinking: ThinkingLevel | None = None
        self.custom_entries: list[tuple[str, dict[str, Any]]] = []
        self.compact_calls: list[str | None] = []

    def abort(self) -> None:
        self.abort_called = True

    def steer(self, text: str) -> None:
        self.steered.append(text)

    def set_model(self, alias: str) -> Any:
        if alias == "missing":
            raise ValueError(f"Model '{alias}' not found in configuration.")
        self.switched_model = alias
        return SimpleNamespace(alias=alias)

    def set_thinking_level(
        self, level: ThinkingLevel | None, *, record: bool = False
    ) -> None:
        self.thinking = level
        if record:
            value = level.value if level is not None else None
            self.custom_entries.append(("thinking_level", {"level": value}))

    def record_custom(self, key: str, value: dict[str, Any]) -> None:
        self.custom_entries.append((key, value))

    async def compact(
        self, instructions: str | None = None, *, reason: CompactionReason = "manual"
    ) -> str:
        self.compact_calls.append(instructions)
        self.stats.context_tokens = 3
        return "summary"


def make_formatter() -> tuple[EventJsonlFormatter, io.StringIO]:
    stream = io.StringIO()
    return EventJsonlFormatter(stream, session_id="control-session"), stream


def events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.mark.asyncio
async def test_abort_command_sets_the_abort_flag() -> None:
    agent = ControlAgent()
    formatter, _ = make_formatter()

    accepted = await dispatch_control_command(agent, {"type": "abort"}, formatter)

    assert accepted is True
    assert agent.abort_called is True


@pytest.mark.asyncio
async def test_steer_command_queues_text() -> None:
    agent = ControlAgent()
    formatter, _ = make_formatter()

    assert await dispatch_control_command(
        agent, {"type": "steer", "message": "focus on tests"}, formatter
    )
    assert agent.steered == ["focus on tests"]


@pytest.mark.asyncio
async def test_steer_without_message_emits_error() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()

    accepted = await dispatch_control_command(agent, {"type": "steer"}, formatter)

    assert accepted is False
    assert agent.steered == []
    assert events(stream)[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_set_model_switches_and_reports_failures() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()

    assert await dispatch_control_command(
        agent, {"type": "set_model", "alias": "fast-model"}, formatter
    )
    assert agent.switched_model == "fast-model"

    assert not await dispatch_control_command(
        agent, {"type": "set_model", "alias": "missing"}, formatter
    )
    errors = [event for event in events(stream) if event["type"] == "error"]
    assert "not found" in errors[-1]["message"]


@pytest.mark.asyncio
async def test_set_thinking_parses_valid_levels_and_records_custom_entry() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()

    assert await dispatch_control_command(
        agent, {"type": "set_thinking", "level": "high"}, formatter
    )
    assert agent.thinking is ThinkingLevel.HIGH
    assert agent.custom_entries[-1] == ("thinking_level", {"level": "high"})

    assert await dispatch_control_command(
        agent, {"type": "set_thinking", "level": "default"}, formatter
    )
    assert agent.thinking is None

    assert not await dispatch_control_command(
        agent, {"type": "set_thinking", "level": "nope"}, formatter
    )
    assert events(stream)[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_compact_runs_and_emits_compaction_event() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()

    accepted = await dispatch_control_command(
        agent, {"type": "compact", "instructions": "keep API notes"}, formatter
    )

    assert accepted is True
    assert agent.compact_calls == ["keep API notes"]
    compacted = events(stream)[-1]
    assert compacted["type"] == "session.compacted"
    assert compacted["tokens_before"] == 10
    assert compacted["tokens_after"] == 3


@pytest.mark.asyncio
async def test_unknown_command_emits_error() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()

    accepted = await dispatch_control_command(agent, {"type": "explode"}, formatter)

    assert accepted is False
    error = events(stream)[-1]
    assert error["type"] == "error"
    assert error["message"] == "Unknown control command: explode"


@pytest.mark.asyncio
async def test_stdin_channel_routes_controls_responses_and_eof() -> None:
    channel = StdinChannel(io.StringIO('{"type":"abort"}\n{"approved":true}\n'))
    channel.start()
    try:
        control = await asyncio.wait_for(channel.next_control(), timeout=2)
        response = await asyncio.wait_for(channel.next_response(), timeout=2)
        end = await asyncio.wait_for(channel.next_control(), timeout=2)
    finally:
        channel.close()

    assert control == {"type": "abort"}
    assert json.loads(response)["approved"] is True
    assert end is None


@pytest.mark.asyncio
async def test_consuming_commands_from_stdin_applies_each_command() -> None:
    agent = ControlAgent()
    formatter, stream = make_formatter()
    channel = StdinChannel(
        io.StringIO(
            '{"type":"steer","message":"go on"}\n'
            '{"type":"set_model","alias":"fast"}\n'
            '{"type":"abort"}\n'
        )
    )
    channel.start()
    try:
        await asyncio.wait_for(
            consume_control_commands(agent, formatter, channel), timeout=2
        )
    finally:
        channel.close()

    assert agent.steered == ["go on"]
    assert agent.switched_model == "fast"
    assert agent.abort_called is True
    assert not [event for event in events(stream) if event["type"] == "error"]
