from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

_locks: dict[Path, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def _canonical(path: Path) -> Path:
    """Resolve symlinks and make the path absolute so aliases share one lock."""
    return path.expanduser().resolve()


@asynccontextmanager
async def file_mutation_lock(path: Path) -> AsyncIterator[None]:
    """Serialize read-modify-write sections that target the same file.

    Different files still proceed in parallel; only mutations of the same
    canonical path are queued. Use this around edit/write operations.
    """
    key = _canonical(path)
    async with _locks_guard:
        lock = _locks.setdefault(key, asyncio.Lock())

    async with lock:
        yield
