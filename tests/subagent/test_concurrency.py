from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.subagent.helpers import (
    GatedBackend,
    HangingBackend,
    make_config,
    make_task_call,
)
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, SubagentConfig
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.subagent import SubAgentRunner
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.tools.builtins.task import TaskArgs
from vibe.core.types import LLMChunk


class DelegateThenHangBackend(FakeBackend):
    """First completion returns a scripted chunk; later ones hang forever."""

    def __init__(self, first: LLMChunk) -> None:
        super().__init__([first])
        self.calls = 0
        self.child_started = asyncio.Event()

    async def complete(self, **kwargs: Any) -> LLMChunk:
        self.calls += 1
        if self.calls == 1:
            return await super().complete(**kwargs)
        self.child_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_exceeding_max_concurrent_returns_error(tmp_path: Path) -> None:
    backend = GatedBackend([mock_llm_chunk(content="done", finish_reason="stop")])
    config = make_config(
        tmp_path, subagent=SubagentConfig(max_concurrent=1, timeout=10)
    )
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    first = asyncio.create_task(runner.run(TaskArgs(description="first", prompt="one")))
    await asyncio.wait_for(backend.started.wait(), timeout=5)

    second = await runner.run(TaskArgs(description="second", prompt="two"))
    assert second.success is False
    assert "too many concurrent subagents" in (second.error or "")

    backend.release.set()
    first_result = await asyncio.wait_for(first, timeout=5)
    assert first_result.success is True
    assert first_result.output == "done"
    assert runner.active_count == 0


@pytest.mark.asyncio
async def test_parent_abort_cancels_child_and_flushes_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    backend = DelegateThenHangBackend(
        mock_llm_chunk(
            content="Delegating.", tool_calls=[make_task_call("slow", "take forever")]
        )
    )
    config = make_config(
        tmp_path,
        session_logging=SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(sessions_dir),
            save_dir=str(tmp_path / "legacy"),
        ),
        subagent=SubagentConfig(timeout=30),
        tools={"task": BaseToolConfig(permission=ToolPermission.ALWAYS)},
    )
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    async def run_parent() -> None:
        async for _ in parent.act("delegate slowly"):
            pass

    act_task = asyncio.create_task(run_parent())
    await asyncio.wait_for(backend.child_started.wait(), timeout=5)

    parent.abort()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(act_task, timeout=5)

    assert runner.active_count == 0

    store = JsonlSessionStore(config.session_logging)
    child_views = [
        store.read(path)
        for path in store.root.glob("*/*.jsonl")
        if store.read(path).header.parent_session_id == parent.session_id
    ]
    assert len(child_views) == 1
    assert child_views[0].header.id != parent.session_id


@pytest.mark.asyncio
async def test_hanging_runner_releases_slot_on_cancellation(tmp_path: Path) -> None:
    backend = HangingBackend()
    config = make_config(tmp_path, subagent=SubagentConfig(timeout=30))
    parent = Agent(config, auto_approve=True, backend=backend)
    runner = SubAgentRunner(parent)

    task = asyncio.create_task(runner.run(TaskArgs(description="x", prompt="y")))
    await asyncio.wait_for(backend.started.wait(), timeout=5)
    assert runner.active_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.active_count == 0
