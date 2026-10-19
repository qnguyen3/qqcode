from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.audit import AuditDecision, AuditLogger, build_audit_logger
from vibe.core.config import AuditConfig, SessionLoggingConfig, VibeConfig
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import FunctionCall, ToolCall


def read_entries(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def expected_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_records_decision_metadata(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl", session_id="session-1", user="alice")

    logger.record(
        "todo", {"action": "read"}, AuditDecision.APPROVED, mode="auto-approve"
    )

    [entry] = read_entries(logger.path)
    assert entry["tool"] == "todo"
    assert entry["decision"] == "approved"
    assert entry["mode"] == "auto-approve"
    assert entry["session_id"] == "session-1"
    assert entry["user"] == "alice"
    assert entry["args_hash"] == expected_hash({"action": "read"})
    assert entry["ts"]


def test_denied_decision_is_recorded(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")

    stored = logger.record(
        "bash", {"command": "rm -rf /"}, AuditDecision.DENIED, mode="interactive"
    )

    assert stored["decision"] == "denied"
    [entry] = read_entries(logger.path)
    assert entry == stored


def test_log_args_false_does_not_leak_full_arguments(tmp_path: Path) -> None:
    secret = "token=super-secret-value"
    logger = AuditLogger(tmp_path / "audit.jsonl", log_args=False)

    logger.record(
        "bash",
        {"command": f"curl {secret}"},
        AuditDecision.APPROVED,
        mode="interactive",
    )

    [entry] = read_entries(logger.path)
    assert "args" not in entry
    assert entry["args_hash"] == expected_hash({"command": f"curl {secret}"})
    assert entry["args_summary"].startswith("curl token=")


def test_log_args_false_omits_nothing_for_non_sensitive_tools(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl", log_args=False)

    logger.record(
        "todo", {"action": "read"}, AuditDecision.APPROVED, mode="interactive"
    )

    [entry] = read_entries(logger.path)
    assert "args" not in entry
    assert "args_summary" not in entry


def test_log_args_true_includes_full_arguments(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl", log_args=True)

    logger.record("bash", {"command": "ls"}, AuditDecision.APPROVED, mode="interactive")

    [entry] = read_entries(logger.path)
    assert entry["args"] == {"command": "ls"}


def test_rotation_keeps_configured_backups(tmp_path: Path) -> None:
    logger = AuditLogger(
        tmp_path / "audit.jsonl", max_bytes=1, keep_files=3, session_id="s"
    )

    for index in range(3):
        logger.record(
            f"tool_{index}", {"n": index}, AuditDecision.APPROVED, mode="interactive"
        )

    assert [entry["tool"] for entry in read_entries(logger.path)] == ["tool_2"]
    assert [entry["tool"] for entry in read_entries(tmp_path / "audit.jsonl.1")] == [
        "tool_1"
    ]
    assert [entry["tool"] for entry in read_entries(tmp_path / "audit.jsonl.2")] == [
        "tool_0"
    ]
    assert not (tmp_path / "audit.jsonl.3").exists()


def test_build_audit_logger_respects_enabled_flag(tmp_path: Path) -> None:
    disabled = AuditConfig(enabled=False, log_path=str(tmp_path / "a.jsonl"))
    enabled = AuditConfig(enabled=True, log_path=str(tmp_path / "a.jsonl"))

    assert build_audit_logger(disabled) is None
    logger = build_audit_logger(enabled, session_id="s1", user="bob")
    assert logger is not None
    assert logger.log_args is False
    assert logger.session_id == "s1"
    assert logger.user == "bob"


def make_todo_call(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id, function=FunctionCall(name="todo", arguments='{"action": "read"}')
    )


def make_agent(
    tmp_path: Path, *, auto_approve: bool, permission: ToolPermission
) -> Agent:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_context_files=False,
        workdir=tmp_path,
        enabled_tools=["todo"],
        tools={"todo": BaseToolConfig(permission=permission)},
        audit=AuditConfig(enabled=True, log_path=str(tmp_path / "audit.jsonl")),
    )
    backend = FakeBackend([
        mock_llm_chunk(content="checking", tool_calls=[make_todo_call("c1")]),
        mock_llm_chunk(content="done", finish_reason="stop"),
    ])
    return Agent(config, auto_approve=auto_approve, backend=backend)


@pytest.mark.asyncio
async def test_agent_records_approved_decision(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, auto_approve=True, permission=ToolPermission.ALWAYS)

    _ = [event async for event in agent.act("check todos")]

    [entry] = read_entries(tmp_path / "audit.jsonl")
    assert entry["tool"] == "todo"
    assert entry["decision"] == "approved"
    assert entry["mode"] == "auto_approve"
    assert entry["session_id"] == agent.session_id


@pytest.mark.asyncio
async def test_agent_records_denied_decision_without_approval_callback(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path, auto_approve=False, permission=ToolPermission.ASK)

    _ = [event async for event in agent.act("check todos")]

    [entry] = read_entries(tmp_path / "audit.jsonl")
    assert entry["tool"] == "todo"
    assert entry["decision"] == "denied"
    assert entry["mode"] == "interactive"
