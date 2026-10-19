from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState


class ConcurrencyProbeArgs(BaseModel):
    label: str = ""


class ConcurrencyProbeResult(BaseModel):
    label: str = ""


class ConcurrencyProbeState(BaseToolState):
    pass


class ConcurrencyProbe(
    BaseTool[
        ConcurrencyProbeArgs,
        ConcurrencyProbeResult,
        BaseToolConfig,
        ConcurrencyProbeState,
    ]
):
    """Appends ``start``/``end`` markers so overlap can be asserted.

    Parallel execution yields ``start:a``, ``start:b``, ``end:a``, ``end:b``;
    serialized execution yields ``start:a``, ``end:a``, ``start:b``, ``end:b``.
    """

    async def run(self, args: ConcurrencyProbeArgs) -> ConcurrencyProbeResult:
        log_path: Path = self.config.effective_workdir / "probe.log"
        _append(log_path, f"start:{args.label}")
        await asyncio.sleep(0.2)
        _append(log_path, f"end:{args.label}")
        return ConcurrencyProbeResult(label=args.label)


def _append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
