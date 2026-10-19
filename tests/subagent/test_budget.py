from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.subagent.helpers import HangingBackend, make_config
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, SubagentConfig
from vibe.core.subagent import SubAgentRunner
from vibe.core.tools.builtins.task import TaskArgs
from vibe.core.types import SubagentEvent, ToolResultEvent


@pytest.mark.asyncio
async def test_max_turns_exceeded_returns_failure(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="step one"),
        mock_llm_chunk(content="step two", finish_reason="stop"),
    ])
    parent = Agent(make_config(tmp_path), auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    result = await runner.run(
        TaskArgs(description="too big", prompt="keep going", max_turns=1)
    )

    assert result.success is False
    assert result.error is not None
    assert "Turn limit of 1 reached" in result.error
    assert result.turns == 1
    assert "failed" in result.format_for_model()


@pytest.mark.asyncio
async def test_timeout_returns_error_and_flushes_child_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    config = make_config(
        tmp_path,
        session_logging=SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(sessions_dir),
            save_dir=str(tmp_path / "legacy"),
        ),
        subagent=SubagentConfig(timeout=0.05),
    )
    backend = HangingBackend()
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    result = await runner.run(TaskArgs(description="hang", prompt="never finish"))

    assert result.success is False
    assert result.error is not None
    assert "timed out" in result.error
    assert runner.active_count == 0

    files = list(sessions_dir.glob("*/*.jsonl"))
    assert len(files) == 1
    assert parent.session_id in files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_price_limit_reports_failure(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(
            content="step one", prompt_tokens=1_000_000, completion_tokens=1_000_000
        ),
        mock_llm_chunk(content="step two", finish_reason="stop"),
    ])
    config = make_config(tmp_path, subagent=SubagentConfig(max_price=0.000001))
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    result = await runner.run(TaskArgs(description="pricey", prompt="go"))

    assert result.success is False
    assert result.error is not None
    assert "Price limit exceeded" in result.error


@pytest.mark.asyncio
async def test_failure_event_emitted_to_parent(tmp_path: Path) -> None:
    backend = FakeBackend([
        mock_llm_chunk(content="step one"),
        mock_llm_chunk(content="step two", finish_reason="stop"),
    ])
    parent = Agent(make_config(tmp_path), auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    await runner.run(TaskArgs(description="small", prompt="go", max_turns=1))

    statuses: list[str] = []
    while not parent._subagent_event_queue.empty():
        event = parent._subagent_event_queue.get_nowait()
        if isinstance(event, SubagentEvent):
            statuses.append(event.status)
        elif isinstance(event, ToolResultEvent):
            statuses.append("tool-result")

    assert "failed" in statuses
