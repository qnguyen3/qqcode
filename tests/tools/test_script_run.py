from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    ScriptToolConfig,
    SessionLoggingConfig,
    ToolRuntimeConfig,
    VibeConfig,
)
from vibe.core.tools.builtins.run_tools_script import (
    RunToolsScript,
    RunToolsScriptArgs,
    RunToolsScriptResult,
)
from vibe.core.tools.manager import ToolManager
from vibe.core.types import FunctionCall, ToolCall, ToolResultEvent


def _make_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
    )


@pytest.mark.asyncio
async def test_script_calls_grep_and_read_file_and_records_tool_calls(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha.py").write_text("# TODO: fix alpha\nx = 1\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("y = 2\n# TODO: fix beta\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("# TODO: ignore text\n", encoding="utf-8")

    config = _make_config(tmp_path)
    manager = ToolManager(config)
    tool = manager.get("run_tools_script")
    assert isinstance(tool, RunToolsScript)

    code = """
paths = [m["path"] for m in tools.grep(pattern="TODO", path=".").matches]
todo = sorted({p for p in paths if p.endswith(".py")})
print(f"{len(todo)} files with TODO")
for p in todo:
    content = tools.read_file(path=p).content
    print(f"{p}: {len(content.splitlines())} lines")
"""
    result = await tool.run(
        RunToolsScriptArgs(code=code, tool_allowlist=["grep", "read_file"])
    )

    assert result.error is None
    assert result.truncated is False
    assert "2 files with TODO" in result.output
    assert "alpha.py: 2 lines" in result.output
    assert "beta.py: 2 lines" in result.output
    assert [call.name for call in result.tool_calls] == [
        "grep",
        "read_file",
        "read_file",
    ]
    assert all(call.ok for call in result.tool_calls)
    assert all(call.duration >= 0.0 for call in result.tool_calls)


@pytest.mark.asyncio
async def test_script_can_use_preloaded_stdlib_and_find_ls(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("a = 10\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("b = 20\n", encoding="utf-8")

    config = _make_config(tmp_path)
    manager = ToolManager(config)
    tool = manager.get("run_tools_script")

    code = """
listing = tools.ls(path=".")
found = tools.find(pattern="*.py", path=".")
py_files = [line for line in found.matches.splitlines() if re.search(r"\\.py$", line)]
data = {
    "count": len(py_files),
    "mean": statistics.mean([2, 4, 6]),
    "root": int(math.sqrt(16)),
    "has_entries": listing["entry_count"] >= 2,
}
print(json.dumps(data, sort_keys=True))
"""
    result = await tool.run(
        RunToolsScriptArgs(code=code, tool_allowlist=["ls", "find"])
    )

    assert result.error is None
    parsed = json.loads(result.output.strip())
    assert parsed == {"count": 2, "has_entries": True, "mean": 4, "root": 4}
    assert [call.name for call in result.tool_calls] == ["ls", "find"]


@pytest.mark.asyncio
async def test_agent_executes_run_tools_script_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")

    config = _make_config(tmp_path)
    call = ToolCall(
        id="call_script_1",
        function=FunctionCall(
            name="run_tools_script",
            arguments=json.dumps({
                "code": 'res = tools.read_file(path="hello.txt")\nprint(res.content.strip().upper())',
                "tool_allowlist": ["read_file"],
            }),
        ),
    )
    backend = FakeBackend([
        mock_llm_chunk(content="Running script.", tool_calls=[call]),
        mock_llm_chunk(content="Done.", finish_reason="stop"),
    ])
    agent = Agent(config, auto_approve=True, backend=backend)

    results = [
        event
        async for event in agent.act("read hello.txt via script")
        if isinstance(event, ToolResultEvent)
    ]

    assert len(results) == 1
    assert results[0].error is None
    assert isinstance(results[0].result, RunToolsScriptResult)
    assert results[0].result.output.strip() == "HELLO WORLD"
    assert [c.name for c in results[0].result.tool_calls] == ["read_file"]
