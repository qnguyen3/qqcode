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


def _get_script_tool(tmp_path: Path):
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
    )
    return ToolManager(config).get("run_tools_script")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "import os\nprint(os.getcwd())",
        "import socket\nprint(socket)",
        "from pathlib import Path\nprint(Path('.'))",
        "__import__('os')",
    ],
)
async def test_sandbox_blocks_imports(tmp_path: Path, code: str) -> None:
    tool = _get_script_tool(tmp_path)

    result = await tool.run(RunToolsScriptArgs(code=code, tool_allowlist=[]))

    assert result.error is not None
    assert "ImportError" in result.error or "import" in result.error.lower()
    assert result.output == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "blocked_name"),
    [
        ("open('/etc/passwd')", "open"),
        ("eval('1 + 1')", "eval"),
        ("exec('x = 1')", "exec"),
        ("compile('1', '<x>', 'eval')", "compile"),
        ("globals()", "globals"),
        ("locals()", "locals"),
        ("getattr(tools, 'read_file')", "getattr"),
    ],
)
async def test_sandbox_blocks_dangerous_builtins(
    tmp_path: Path, code: str, blocked_name: str
) -> None:
    tool = _get_script_tool(tmp_path)

    result = await tool.run(RunToolsScriptArgs(code=code, tool_allowlist=[]))

    assert result.error is not None
    assert blocked_name in result.error
    assert result.output == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "print(().__class__.__base__.__subclasses__())",
        "print(json.__dict__)",
        "print(tools.__class__)",
    ],
)
async def test_sandbox_blocks_dunder_escape_attempts(tmp_path: Path, code: str) -> None:
    tool = _get_script_tool(tmp_path)

    result = await tool.run(RunToolsScriptArgs(code=code, tool_allowlist=[]))

    assert result.error is not None
    assert "dunder" in result.error.lower() or "AttributeError" in result.error
    assert result.output == ""


@pytest.mark.asyncio
async def test_sandbox_has_no_network_or_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPER_SECRET_TEST_TOKEN", "do-not-leak")
    tool = _get_script_tool(tmp_path)

    result = await tool.run(
        RunToolsScriptArgs(
            code="import urllib.request\nurllib.request.urlopen('http://127.0.0.1')",
            tool_allowlist=[],
        )
    )

    assert result.error is not None
    assert "ImportError" in result.error
