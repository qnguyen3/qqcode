from __future__ import annotations

from pathlib import Path
import sys
from typing import Literal

from vibe.core.config import MCPStdio

FAKE_SERVER = Path(__file__).parent / "fake_server.py"


def stdio_server(
    name: str = "fake",
    *,
    state_file: Path | None = None,
    timeout: float = 10.0,
    enabled: bool = True,
    prompt: str | None = None,
    tool_exposure: Literal["direct", "deferred", "hidden"] = "direct",
) -> MCPStdio:
    args = [str(FAKE_SERVER)]
    if state_file is not None:
        args.append(str(state_file))
    return MCPStdio(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=args,
        timeout=timeout,
        enabled=enabled,
        prompt=prompt,
        tool_exposure=tool_exposure,
    )
