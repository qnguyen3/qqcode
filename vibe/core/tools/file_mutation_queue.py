from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class FileMutationQueue:
    """Serialize mutations per resolved path; shared by write_file/edit."""

    def __init__(self) -> None:
        self._locks: dict[Path, asyncio.Lock] = {}

    async def run(self, path: Path, coro_factory: Callable[[], Awaitable[T]]) -> T:
        lock = self._locks.setdefault(path.resolve(), asyncio.Lock())
        async with lock:
            return await coro_factory()

    def reset(self) -> None:
        self._locks.clear()


_default_queue: FileMutationQueue | None = None


def get_default_file_mutation_queue() -> FileMutationQueue:
    global _default_queue
    if _default_queue is None:
        _default_queue = FileMutationQueue()
    return _default_queue


def reset_default_file_mutation_queue() -> None:
    global _default_queue
    _default_queue = None
