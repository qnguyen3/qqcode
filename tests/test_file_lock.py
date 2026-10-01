from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibe.core.utils.file_lock import file_mutation_lock


@pytest.mark.asyncio
async def test_same_file_mutations_serialize(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("0", encoding="utf-8")
    events: list[str] = []

    async def worker(index: int, delay: float) -> None:
        async with file_mutation_lock(target):
            events.append(f"enter-{index}")
            await asyncio.sleep(delay)
            events.append(f"exit-{index}")

    await asyncio.gather(worker(0, 0.05), worker(1, 0.0))

    assert events == ["enter-0", "exit-0", "enter-1", "exit-1"]


@pytest.mark.asyncio
async def test_same_file_through_symlink_shares_the_lock(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("0", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(target)

    events: list[str] = []

    async def worker(name: str, path: Path) -> None:
        async with file_mutation_lock(path):
            events.append(f"enter-{name}")
            await asyncio.sleep(0.05)
            events.append(f"exit-{name}")

    await asyncio.gather(worker("real", target), worker("alias", alias))

    assert events == ["enter-real", "exit-real", "enter-alias", "exit-alias"]


@pytest.mark.asyncio
async def test_different_files_do_not_block_each_other(tmp_path: Path) -> None:
    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "b.txt"
    path_a.write_text("a", encoding="utf-8")
    path_b.write_text("b", encoding="utf-8")

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with file_mutation_lock(path_a):
            first_entered.set()
            await asyncio.wait_for(second_entered.wait(), timeout=1.0)

    async def second() -> None:
        await first_entered.wait()
        async with file_mutation_lock(path_b):
            second_entered.set()

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=2.0)
