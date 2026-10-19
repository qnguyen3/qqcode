from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.edit import Edit, EditArgs, EditConfig, EditState
from vibe.core.tools.file_mutation_queue import FileMutationQueue
from vibe.core.tools.manager import ToolManager


@pytest.mark.asyncio
async def test_queue_serializes_calls_for_the_same_path(tmp_path: Path) -> None:
    queue = FileMutationQueue()
    order: list[str] = []

    async def first() -> int:
        order.append("first-start")
        await asyncio.sleep(0.05)
        order.append("first-end")
        return 1

    async def second() -> int:
        order.append("second-start")
        return 2

    results = await asyncio.gather(
        queue.run(tmp_path / "a.txt", first), queue.run(tmp_path / "a.txt", second)
    )

    assert results == [1, 2]
    assert order == ["first-start", "first-end", "second-start"]


@pytest.mark.asyncio
async def test_queue_does_not_block_different_paths(tmp_path: Path) -> None:
    queue = FileMutationQueue()
    order: list[str] = []

    async def first() -> None:
        order.append("first-start")
        await asyncio.sleep(0.05)
        order.append("first-end")

    async def second() -> None:
        order.append("second-start")

    await asyncio.gather(
        queue.run(tmp_path / "a.txt", first), queue.run(tmp_path / "b.txt", second)
    )

    assert order == ["first-start", "second-start", "first-end"]


class InstrumentedEdit(Edit):
    def __init__(
        self, config: EditConfig, state: EditState, label: str, recorder: list[str]
    ) -> None:
        super().__init__(config, state)
        self.label = label
        self.recorder = recorder

    async def _read_file(self, file_path: Path) -> str:
        self.recorder.append(f"{self.label}-read")
        await asyncio.sleep(0.05)
        return await super()._read_file(file_path)

    async def _write_file(self, file_path: Path, content: str) -> None:
        self.recorder.append(f"{self.label}-write")
        await asyncio.sleep(0.05)
        await super()._write_file(file_path, content)


def _make_instrumented_edit(
    config: EditConfig, queue: FileMutationQueue, recorder: list[str], label: str
) -> InstrumentedEdit:
    return InstrumentedEdit(
        config=config,
        state=EditState(file_mutation_queue=queue),
        label=label,
        recorder=recorder,
    )


@pytest.mark.asyncio
async def test_concurrent_edits_to_the_same_file_serialize(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("A\n")
    queue = FileMutationQueue()
    recorder: list[str] = []
    config = EditConfig(workdir=tmp_path)

    first = _make_instrumented_edit(config, queue, recorder, "first")
    second = _make_instrumented_edit(config, queue, recorder, "second")

    results = await asyncio.gather(
        first.run(EditArgs(file_path=str(target), old_string="A", new_string="ONE")),
        second.run(EditArgs(file_path=str(target), old_string="A", new_string="TWO")),
        return_exceptions=True,
    )

    # The second edit must read after the first edit wrote; it then fails
    # because "A" is gone. Without the queue both reads would happen first
    # and both edits would succeed.
    assert any(isinstance(result, ToolError) for result in results)
    assert target.read_text() in {"ONE\n", "TWO\n"}

    winner = (
        "first"
        if recorder.index("first-write") < recorder.index("second-read")
        else "second"
    )
    assert recorder.index(f"{winner}-read") < recorder.index(f"{winner}-write")
    assert recorder.index(f"{winner}-write") < recorder.index(
        f"{'second' if winner == 'first' else 'first'}-read"
    )


def test_tool_manager_injects_shared_queue_into_mutating_tools() -> None:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
    )
    manager = ToolManager(config)

    edit = manager.get("edit")
    write_file = manager.get("write_file")

    assert edit.state.file_mutation_queue is manager.file_mutation_queue
    assert write_file.state.file_mutation_queue is manager.file_mutation_queue
