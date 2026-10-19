from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.tools.base import BaseToolState
from vibe.core.tools.builtins.bash import Bash, BashArgs, BashToolConfig
from vibe.core.tools.truncate import BASH_MAX_LINES


@pytest.mark.asyncio
async def test_bash_keeps_tail_and_spills_full_output(tmp_path: Path) -> None:
    config = BashToolConfig(workdir=tmp_path)
    tool = Bash(config=config, state=BaseToolState())

    result = await tool.run(BashArgs(command="seq 1 100000"))

    lines = result.stdout.splitlines()
    assert len(lines) <= BASH_MAX_LINES
    assert len(lines) < 100_000
    assert lines[-1] == "100000"
    assert lines[0] != "1"
    assert result.truncated is True
    assert result.returncode == 0

    assert result.full_output_path is not None
    assert result.notice is not None
    assert "Full output:" in result.notice
    assert result.full_output_path in result.notice

    full_path = Path(result.full_output_path)
    assert full_path.exists()
    full_lines = full_path.read_text("utf-8").splitlines()
    assert full_lines[0] == "1"
    assert full_lines[-1] == "100000"

    full_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_bash_does_not_spill_small_output(tmp_path: Path) -> None:
    config = BashToolConfig(workdir=tmp_path)
    tool = Bash(config=config, state=BaseToolState())

    result = await tool.run(BashArgs(command="echo hello"))

    assert result.stdout == "hello\n"
    assert result.truncated is False
    assert result.full_output_path is None
    assert result.notice is None
