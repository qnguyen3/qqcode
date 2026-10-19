from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import (
    BaseToolConfig,
    ToolPermission,
    check_path_patterns,
    resolve_tool_path,
)
from vibe.core.tools.builtins.edit import EditConfig
from vibe.core.tools.builtins.write_file import WriteFileConfig
from vibe.core.types import FunctionCall, ToolCall, ToolResultEvent


def tool_call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments))
    )


def make_agent(
    tmp_path: Path, tool_name: str, tool_config: BaseToolConfig, call: ToolCall
) -> Agent:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        enabled_tools=[tool_name],
        tools={tool_name: tool_config},
    )
    backend = FakeBackend([
        mock_llm_chunk(content="working", tool_calls=[call]),
        mock_llm_chunk(content="done", finish_reason="stop"),
    ])
    return Agent(config, auto_approve=True, backend=backend)


async def collect_tool_results(agent: Agent) -> list[ToolResultEvent]:
    return [
        event
        async for event in agent.act("do it")
        if isinstance(event, ToolResultEvent)
    ]


def test_check_path_patterns_denylist_wins(tmp_path: Path) -> None:
    target = tmp_path / "secrets" / "key.txt"

    decision = check_path_patterns(
        target, allowlist=[str(tmp_path / "**")], denylist=[str(target)]
    )

    assert decision is ToolPermission.NEVER


def test_check_path_patterns_allowlist_requires_match(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed" / "ok.txt"

    assert (
        check_path_patterns(
            allowed, allowlist=[str(tmp_path / "allowed" / "**")], denylist=[]
        )
        is ToolPermission.ALWAYS
    )
    assert (
        check_path_patterns(
            tmp_path / "other.txt",
            allowlist=[str(tmp_path / "allowed" / "**")],
            denylist=[],
        )
        is ToolPermission.NEVER
    )
    assert (
        check_path_patterns(tmp_path / "other.txt", allowlist=[], denylist=[]) is None
    )


def test_resolve_tool_path_is_absolute(tmp_path: Path) -> None:
    resolved = resolve_tool_path("sub/file.txt", tmp_path)

    assert resolved == (tmp_path / "sub" / "file.txt").resolve()


@pytest.mark.asyncio
async def test_write_outside_path_allowlist_is_denied(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    blocked = tmp_path / "blocked.txt"
    config = WriteFileConfig(
        permission=ToolPermission.ALWAYS,
        workdir=tmp_path,
        path_allowlist=[str(allowed / "**")],
    )
    agent = make_agent(
        tmp_path,
        "write_file",
        config,
        tool_call("w_denied", "write_file", {"path": str(blocked), "content": "nope"}),
    )

    results = await collect_tool_results(agent)

    assert len(results) == 1
    assert results[0].skipped is True
    assert "path restrictions" in (results[0].skip_reason or "")
    assert not blocked.exists()


@pytest.mark.asyncio
async def test_write_inside_path_allowlist_executes(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "ok.txt"
    config = WriteFileConfig(
        permission=ToolPermission.ALWAYS,
        workdir=tmp_path,
        path_allowlist=[str(allowed / "**")],
    )
    agent = make_agent(
        tmp_path,
        "write_file",
        config,
        tool_call("w_ok", "write_file", {"path": str(target), "content": "hello"}),
    )

    results = await collect_tool_results(agent)

    assert len(results) == 1
    assert results[0].skipped is False
    assert results[0].error is None
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_edit_outside_path_allowlist_is_denied(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("original", encoding="utf-8")
    config = EditConfig(
        permission=ToolPermission.ALWAYS,
        workdir=tmp_path,
        path_allowlist=[str(allowed / "**")],
    )
    agent = make_agent(
        tmp_path,
        "edit",
        config,
        tool_call(
            "e_denied",
            "edit",
            {
                "file_path": str(blocked),
                "old_string": "original",
                "new_string": "changed",
            },
        ),
    )

    results = await collect_tool_results(agent)

    assert len(results) == 1
    assert results[0].skipped is True
    assert "path restrictions" in (results[0].skip_reason or "")
    assert blocked.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_path_denylist_overrides_allowlist(tmp_path: Path) -> None:
    target = tmp_path / "blocked.txt"
    config = WriteFileConfig(
        permission=ToolPermission.ALWAYS,
        workdir=tmp_path,
        path_allowlist=[str(tmp_path / "**")],
        path_denylist=[str(target)],
    )
    agent = make_agent(
        tmp_path,
        "write_file",
        config,
        tool_call("w_deny", "write_file", {"path": str(target), "content": "no"}),
    )

    results = await collect_tool_results(agent)

    assert results[0].skipped is True
    assert not target.exists()
