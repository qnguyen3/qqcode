from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    DebugConfig,
    SessionLoggingConfig,
    TelemetryConfig,
    VibeConfig,
)
import vibe.core.metrics as metrics_module
from vibe.core.metrics import (
    COUNTER_TURNS,
    MetricsRecorder,
    atomic_write_json,
    build_debug_logger,
    build_metrics_recorder,
)


def read_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_recorder(tmp_path: Path, *, flush_interval: float = 0.0) -> MetricsRecorder:
    return MetricsRecorder(tmp_path / "metrics.json", flush_interval=flush_interval)


def test_increment_updates_counters_and_writes_file(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)

    recorder.increment(COUNTER_TURNS)
    recorder.increment(COUNTER_TURNS, 2)
    recorder.flush()

    assert recorder.snapshot() == {"turns": 3}
    payload = read_payload(tmp_path / "metrics.json")
    assert payload["version"] == 1
    assert payload["counters"] == {"turns": 3}
    assert payload["updated_at"]


def test_flush_interval_defers_disk_writes(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path, flush_interval=3600.0)

    recorder.increment(COUNTER_TURNS)

    assert not (tmp_path / "metrics.json").exists()
    assert recorder.snapshot() == {"turns": 1}

    recorder.flush()

    assert read_payload(tmp_path / "metrics.json")["counters"] == {"turns": 1}


def test_atomic_write_json_creates_directories_and_replaces(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "metrics.json"

    atomic_write_json(path, {"value": 1})
    atomic_write_json(path, {"value": 2})

    assert read_payload(path) == {"value": 2}
    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_json_preserves_previous_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metrics.json"
    path.write_text('{"value": 1}', encoding="utf-8")

    def exploding_dump(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(metrics_module.json, "dump", exploding_dump)

    with pytest.raises(RuntimeError, match="disk on fire"):
        atomic_write_json(path, {"value": 2})

    assert read_payload(path) == {"value": 1}
    leftovers = [
        p.name for p in path.parent.iterdir() if p.name.startswith(".metrics.json")
    ]
    assert leftovers == []


def test_build_metrics_recorder_respects_mode(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    off = TelemetryConfig(mode="off", metrics_path=str(path))
    local = TelemetryConfig(mode="local", metrics_path=str(path), flush_interval=0.0)

    assert build_metrics_recorder(off) is None

    recorder = build_metrics_recorder(local)
    assert recorder is not None
    recorder.increment(COUNTER_TURNS)
    assert read_payload(path)["counters"] == {"turns": 1}


def test_build_debug_logger_records_metadata_only(tmp_path: Path) -> None:
    path = tmp_path / "debug.jsonl"
    enabled = DebugConfig(enabled=True, log_path=str(path))

    assert build_debug_logger(DebugConfig(enabled=False, log_path=str(path))) is None

    logger = build_debug_logger(enabled)
    assert logger is not None
    logger.log_request(
        model="eval-model",
        message_count=3,
        estimated_tokens=42,
        duration=0.5,
        tool_count=2,
    )
    [entry] = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entry["event"] == "request"
    assert entry["model"] == "eval-model"
    assert entry["estimated_tokens"] == 42
    assert "messages" not in entry
    assert "content" not in entry


@pytest.mark.asyncio
async def test_agent_records_turn_metric(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        enabled_tools=["todo"],
        telemetry=TelemetryConfig(
            mode="local", metrics_path=str(path), flush_interval=0.0
        ),
    )
    backend = FakeBackend([mock_llm_chunk(content="done", finish_reason="stop")])
    agent = Agent(config, auto_approve=True, backend=backend)

    _ = [event async for event in agent.act("hello")]

    assert read_payload(path)["counters"].get(COUNTER_TURNS, 0) >= 1
