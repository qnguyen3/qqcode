from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.config import (
    ScriptToolConfig,
    SessionLoggingConfig,
    ToolRuntimeConfig,
    VibeConfig,
)
from vibe.core.tools.builtins.run_tools_script import RunToolsScriptArgs
from vibe.core.tools.manager import ToolManager


def _make_script_tool(
    tmp_path: Path,
    *,
    timeout: float = 5.0,
    max_calls: int = 20,
    max_output_bytes: int = 16384,
    memory_mb: int = 256,
):
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(
            script=ScriptToolConfig(
                enabled=True,
                timeout=timeout,
                max_calls=max_calls,
                max_output_bytes=max_output_bytes,
                memory_mb=memory_mb,
            )
        ),
    )
    return ToolManager(config).get("run_tools_script")


@pytest.mark.asyncio
async def test_script_timeout_kills_worker_and_reports_reason(tmp_path: Path) -> None:
    tool = _make_script_tool(tmp_path, timeout=0.3)

    result = await tool.run(
        RunToolsScriptArgs(
            code="print('started')\nwhile True:\n    pass\n", tool_allowlist=[]
        )
    )

    assert result.error is not None
    assert "timed out" in result.error.lower()
    assert "started" in result.output


@pytest.mark.asyncio
async def test_script_exceeding_max_calls_stops_safely(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    tool = _make_script_tool(tmp_path, max_calls=3)

    code = """
for _ in range(10):
    try:
        tools.ls(path=".")
    except Exception:
        pass
print("should not reach here")
"""
    result = await tool.run(RunToolsScriptArgs(code=code, tool_allowlist=["ls"]))

    assert result.error is not None
    assert "max_calls" in result.error
    assert "should not reach here" not in result.output
    ok_calls = [c for c in result.tool_calls if c.ok]
    failed_calls = [c for c in result.tool_calls if not c.ok]
    assert len(ok_calls) == 3
    assert len(failed_calls) == 1


@pytest.mark.asyncio
async def test_script_exceeding_max_output_bytes_truncates_and_stops(
    tmp_path: Path,
) -> None:
    tool = _make_script_tool(tmp_path, max_output_bytes=512)

    code = """
for i in range(100):
    print("line-" + ("x" * 50))
"""
    result = await tool.run(RunToolsScriptArgs(code=code, tool_allowlist=[]))

    assert result.truncated is True
    assert len(result.output.encode("utf-8")) <= 512
    assert result.error is not None
    assert "max_output_bytes" in result.error


@pytest.mark.asyncio
async def test_script_exceeding_memory_limit_reports_memory_error(
    tmp_path: Path,
) -> None:
    tool = _make_script_tool(tmp_path, memory_mb=64)

    result = await tool.run(
        RunToolsScriptArgs(
            code="big = 'a' * (200 * 1024 * 1024)\nprint(len(big))", tool_allowlist=[]
        )
    )

    assert result.error is not None
    assert "memory" in result.error.lower()
