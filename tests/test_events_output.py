from __future__ import annotations

import io
import json

from pydantic import BaseModel
import pytest

from vibe.core.output_formatters import EventJsonlFormatter, create_formatter
from vibe.core.tools.builtins.edit import Edit, EditArgs
from vibe.core.types import (
    AssistantEvent,
    CompactEndEvent,
    OutputFormat,
    ToolCallEvent,
    ToolResultEvent,
)


class SimpleResult(BaseModel):
    ok: bool = True
    message: str = "done"


def make_formatter() -> tuple[EventJsonlFormatter, io.StringIO]:
    stream = io.StringIO()
    return EventJsonlFormatter(stream, session_id="session-123"), stream


def parsed_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_every_line_carries_schema_and_session_started_comes_first() -> None:
    formatter, stream = make_formatter()

    events = parsed_lines(stream)

    assert events[0]["type"] == "session.started"
    assert events[0]["schema"] == 1
    assert events[0]["session_id"] == "session-123"
    assert isinstance(events[0]["timestamp"], str)
    assert all(event["schema"] == 1 for event in events)


def test_assistant_events_emit_turn_delta_and_completion() -> None:
    formatter, stream = make_formatter()

    formatter.on_event(
        AssistantEvent(
            content="Hello world",
            prompt_tokens=120,
            completion_tokens=42,
            cache_read_tokens=7,
            cache_write_tokens=3,
            cost_usd=0.0015,
        )
    )

    events = parsed_lines(stream)
    types = [event["type"] for event in events]
    assert types == [
        "session.started",
        "turn.started",
        "message.delta",
        "turn.completed",
    ]

    turn_started = events[1]
    assert turn_started["turn"] == 1

    delta = events[2]
    assert delta["delta"] == "Hello world"
    assert delta["content"] == "Hello world"
    assert delta["item_id"].startswith("msg_")

    completed = events[3]
    assert completed["usage"] == {
        "input": 120,
        "output": 42,
        "cache_read": 7,
        "cache_write": 3,
    }
    assert completed["cost_usd"] == 0.0015


def test_multiple_turns_increment_the_turn_counter() -> None:
    formatter, stream = make_formatter()

    formatter.on_event(AssistantEvent(content="one", completion_tokens=1))
    formatter.on_event(AssistantEvent(content="two", completion_tokens=1))

    turns = [event for event in parsed_lines(stream) if event["type"] == "turn.started"]
    assert [event["turn"] for event in turns] == [1, 2]


def test_tool_call_and_result_events() -> None:
    formatter, stream = make_formatter()

    formatter.on_event(
        ToolCallEvent(
            tool_name="edit",
            tool_call_id="call-1",
            args=EditArgs(file_path="/tmp/test.txt", old_string="a", new_string="b"),
            tool_class=Edit,
        )
    )
    formatter.on_event(
        ToolResultEvent(
            tool_name="bash",
            tool_class=None,
            result=SimpleResult(ok=True, message="fine"),
            tool_call_id="call-2",
        )
    )
    formatter.on_event(
        ToolResultEvent(
            tool_name="bash", tool_class=None, error="boom", tool_call_id="call-3"
        )
    )
    formatter.on_event(
        ToolResultEvent(
            tool_name="bash",
            tool_class=None,
            skipped=True,
            skip_reason="not allowed",
            tool_call_id="call-4",
        )
    )

    events = parsed_lines(stream)
    tool_call = next(event for event in events if event["type"] == "tool.call")
    assert tool_call["tool_call_id"] == "call-1"
    assert tool_call["tool_name"] == "edit"
    assert tool_call["args"]["file_path"] == "/tmp/test.txt"

    results = [event for event in events if event["type"] == "tool.result"]
    assert [result["status"] for result in results] == ["ok", "error", "skipped"]
    assert results[0]["text"]
    assert results[1]["text"] == "boom"
    assert results[2]["text"] == "not allowed"
    assert all(result["truncated"] is False for result in results)


def test_compaction_event_uses_before_and_after_tokens() -> None:
    formatter, stream = make_formatter()

    formatter.on_event(
        CompactEndEvent(
            old_context_tokens=1000, new_context_tokens=120, summary_length=42
        )
    )

    compacted = parsed_lines(stream)[-1]
    assert compacted["type"] == "session.compacted"
    assert compacted["tokens_before"] == 1000
    assert compacted["tokens_after"] == 120


def test_error_and_escaping_roundtrip() -> None:
    formatter, stream = make_formatter()

    tricky = 'line1\nline2 "quoted" \\ slash \u00e9 \U0001f600'
    formatter.on_event(AssistantEvent(content=tricky, completion_tokens=1))
    formatter.emit_error("something\nbad")

    events = parsed_lines(stream)
    delta = next(event for event in events if event["type"] == "message.delta")
    assert delta["delta"] == tricky
    assert delta["content"] == tricky

    error = events[-1]
    assert error["type"] == "error"
    assert error["message"] == "something\nbad"


def test_create_formatter_selects_events_formatter() -> None:
    stream = io.StringIO()
    formatter = create_formatter(OutputFormat.EVENTS, stream=stream, session_id="abc")

    assert isinstance(formatter, EventJsonlFormatter)
    assert formatter.session_id == "abc"
    assert json.loads(stream.getvalue().splitlines()[0])["type"] == "session.started"


def test_run_programmatic_emits_event_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.mock.mock_backend_factory import mock_backend_factory
    from tests.mock.utils import mock_llm_chunk
    from tests.stubs.fake_backend import FakeBackend
    from vibe.core import output_formatters, run_programmatic
    from vibe.core.config import Backend, SessionLoggingConfig, VibeConfig

    captured = io.StringIO()
    original = output_formatters.create_formatter

    def factory(format_type, stream=None, session_id=None):
        return original(format_type, stream=captured, session_id=session_id)

    monkeypatch.setattr("vibe.core.programmatic.create_formatter", factory)

    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([
            mock_llm_chunk(content="streamed answer", finish_reason="stop")
        ]),
    ):
        config = VibeConfig(
            session_logging=SessionLoggingConfig(enabled=False),
            system_prompt_id="tests",
            include_project_context=False,
            include_prompt_detail=False,
            include_model_info=False,
            include_commit_signature=False,
        )
        run_programmatic(
            config=config,
            prompt="hello",
            output_format=OutputFormat.EVENTS,
            mode="auto-approve",
        )

    parsed = parsed_lines(captured)
    types = [event["type"] for event in parsed]
    assert types[0] == "session.started"
    assert "turn.started" in types
    assert "message.delta" in types
    assert types[-1] == "turn.completed"
    delta = next(event for event in parsed if event["type"] == "message.delta")
    assert delta["delta"] == "streamed answer"
    assert all(event["schema"] == 1 for event in parsed)
