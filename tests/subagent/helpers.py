from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.types import FunctionCall, LLMChunk, ToolCall


def make_config(tmp_path: Path, **overrides: Any) -> VibeConfig:
    defaults: dict[str, Any] = {
        "session_logging": SessionLoggingConfig(enabled=False),
        "auto_compact_threshold": 0,
        "system_prompt_id": "tests",
        "include_project_context": False,
        "include_prompt_detail": False,
        "include_context_files": False,
        "workdir": tmp_path,
        "enabled_tools": ["task"],
    }
    return VibeConfig(**{**defaults, **overrides})


def make_task_call(
    description: str = "do work",
    prompt: str = "do the work",
    *,
    call_id: str = "task_1",
    max_turns: int | None = 15,
) -> ToolCall:
    args: dict[str, Any] = {"description": description, "prompt": prompt}
    if max_turns is not None:
        args["max_turns"] = max_turns
    return ToolCall(
        id=call_id, function=FunctionCall(name="task", arguments=json.dumps(args))
    )


class HangingBackend(FakeBackend):
    """Backend that blocks forever on every completion request."""

    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def complete(self, **kwargs: Any) -> LLMChunk:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class GatedBackend(FakeBackend):
    """Backend that blocks until ``release`` is set, then serves its chunks."""

    def __init__(self, chunks: list[LLMChunk]) -> None:
        super().__init__(chunks)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, **kwargs: Any) -> LLMChunk:
        self.started.set()
        await self.release.wait()
        return await super().complete(**kwargs)
