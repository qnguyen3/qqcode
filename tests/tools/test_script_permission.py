from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    AuditConfig,
    ScriptToolConfig,
    SessionLoggingConfig,
    ToolRuntimeConfig,
    VibeConfig,
)
from vibe.core.hooks.events import HookDecision, ToolCallEventHook
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.tools.builtins.bash import Bash
from vibe.core.tools.builtins.edit import Edit
from vibe.core.tools.builtins.find import Find
from vibe.core.tools.builtins.grep import Grep
from vibe.core.tools.builtins.ls import Ls
from vibe.core.tools.builtins.read_file import ReadFile, ReadFileToolConfig
from vibe.core.tools.builtins.run_tools_script import RunToolsScript, RunToolsScriptArgs
from vibe.core.tools.builtins.write_file import WriteFile
from vibe.core.tools.manager import ToolManager
from vibe.core.utils import ApprovalResponse


def _read_audit(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_scriptable_classvar_defaults() -> None:
    assert ReadFile.scriptable is True
    assert Grep.scriptable is True
    assert Find.scriptable is True
    assert Ls.scriptable is True
    assert WriteFile.scriptable is False
    assert Edit.scriptable is False
    assert Bash.scriptable is False
    assert RunToolsScript.scriptable is False


@pytest.mark.asyncio
async def test_tool_outside_allowlist_is_rejected_and_audited(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("top-secret", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"

    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
        audit=AuditConfig(enabled=True, log_path=str(audit_path)),
    )
    tool = ToolManager(config).get("run_tools_script")

    result = await tool.run(
        RunToolsScriptArgs(
            code='print(tools.read_file(path="secret.txt").content)',
            tool_allowlist=["ls"],
        )
    )

    assert result.error is not None
    assert "tool_allowlist" in result.error
    assert "top-secret" not in result.output
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].ok is False

    entries = _read_audit(audit_path)
    assert any(e["tool"] == "read_file" and e["decision"] == "denied" for e in entries)


@pytest.mark.asyncio
async def test_non_scriptable_tool_in_allowlist_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "pwned.txt"
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
    )
    tool = ToolManager(config).get("run_tools_script")

    result = await tool.run(
        RunToolsScriptArgs(
            code=f'tools.write_file(path="{target}", content="bad")',
            tool_allowlist=["write_file"],
        )
    )

    assert result.error is not None
    assert "not scriptable" in result.error.lower()
    assert not target.exists()


@pytest.mark.asyncio
async def test_tool_requiring_interactive_approval_does_not_auto_run(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("sensitive", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"
    callback_called = False

    def approval_callback(
        _tool_name: str, _args: dict[str, Any], _call_id: str
    ) -> tuple[ApprovalResponse, str | None]:
        nonlocal callback_called
        callback_called = True
        return ApprovalResponse.YES, None

    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
        tools={"read_file": BaseToolConfig(permission=ToolPermission.ASK)},
        audit=AuditConfig(enabled=True, log_path=str(audit_path)),
    )
    agent = Agent(
        config,
        auto_approve=False,
        backend=FakeBackend([mock_llm_chunk(content="ok", finish_reason="stop")]),
    )
    agent.set_approval_callback(approval_callback)

    script_tool = agent._get_tool_instance("run_tools_script")
    result = await script_tool.run(
        RunToolsScriptArgs(
            code='print(tools.read_file(path="data.txt").content)',
            tool_allowlist=["read_file"],
        )
    )

    assert callback_called is False
    assert result.error is not None
    assert "approval" in result.error.lower() or "ask" in result.error.lower()
    assert "sensitive" not in result.output

    entries = _read_audit(audit_path)
    assert any(e["tool"] == "read_file" and e["decision"] == "denied" for e in entries)


@pytest.mark.asyncio
async def test_tool_with_ask_permission_runs_when_agent_is_auto_approve(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("approved-data\n", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"

    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
        tools={"read_file": BaseToolConfig(permission=ToolPermission.ASK)},
        audit=AuditConfig(enabled=True, log_path=str(audit_path)),
    )
    agent = Agent(
        config,
        auto_approve=True,
        backend=FakeBackend([mock_llm_chunk(content="ok", finish_reason="stop")]),
    )

    script_tool = agent._get_tool_instance("run_tools_script")
    result = await script_tool.run(
        RunToolsScriptArgs(
            code='print(tools.read_file(path="data.txt").content.strip())',
            tool_allowlist=["read_file"],
        )
    )

    assert result.error is None
    assert result.output.strip() == "approved-data"
    entries = _read_audit(audit_path)
    assert any(
        e["tool"] == "read_file" and e["decision"] == "approved" for e in entries
    )


@pytest.mark.asyncio
async def test_path_denylist_and_hooks_enforced_inside_script(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    public = tmp_path / "public.txt"
    public.write_text("public", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"

    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
        tools={
            "read_file": ReadFileToolConfig(
                permission=ToolPermission.ALWAYS, path_denylist=[str(secret)]
            )
        },
        audit=AuditConfig(enabled=True, log_path=str(audit_path)),
    )
    agent = Agent(
        config,
        auto_approve=True,
        backend=FakeBackend([mock_llm_chunk(content="ok", finish_reason="stop")]),
    )

    seen_hook_calls: list[str] = []

    def on_tool_call(event: ToolCallEventHook) -> HookDecision:
        seen_hook_calls.append(event.tool_name)
        return HookDecision(action="allow")

    agent.hooks.register("tool_call", on_tool_call)

    script_tool = agent._get_tool_instance("run_tools_script")
    denied_res = await script_tool.run(
        RunToolsScriptArgs(
            code=f'print(tools.read_file(path="{secret}").content)',
            tool_allowlist=["read_file"],
        )
    )

    assert denied_res.error is not None
    assert "path restrictions" in denied_res.error
    assert seen_hook_calls == ["read_file"]
    entries = _read_audit(audit_path)
    assert any(
        e["tool"] == "read_file" and e["decision"] == "denylist" for e in entries
    )
