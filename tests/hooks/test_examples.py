from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from vibe.core.hooks.events import (
    AgentEndEvent,
    BeforeRequestEvent,
    ToolCallEventHook,
    ToolResultEventHook,
)
from vibe.core.hooks.loader import load_hook_file
from vibe.core.hooks.registry import HookRegistry
from vibe.core.types import LLMUsage

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "hooks"


def load_example(name: str) -> HookRegistry:
    registry = HookRegistry()
    assert load_hook_file(EXAMPLES_DIR / name, registry)
    return registry


def test_protected_paths_example_denies_sensitive_writes(tmp_path: Path) -> None:
    registry = load_example("protected_paths.py")

    env_event = ToolCallEventHook(
        tool_name="write_file",
        tool_call_id="1",
        args={"path": str(tmp_path / ".env"), "content": "SECRET=1"},
    )
    assert asyncio.run(registry.dispatch(env_event)).action == "deny"

    secrets_event = ToolCallEventHook(
        tool_name="edit",
        tool_call_id="2",
        args={"file_path": "secrets/token.txt", "old_string": "a", "new_string": "b"},
    )
    assert asyncio.run(registry.dispatch(secrets_event)).action == "deny"

    safe_event = ToolCallEventHook(
        tool_name="write_file",
        tool_call_id="3",
        args={"path": "src/main.py", "content": "print()"},
    )
    assert asyncio.run(registry.dispatch(safe_event)).action == "allow"


def test_audit_log_example_appends_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / "home"))
    registry = load_example("audit_log.py")

    call_event = ToolCallEventHook(
        tool_name="bash", tool_call_id="call_1", args={"command": "ls"}
    )
    result_event = ToolResultEventHook(
        tool_name="bash", tool_call_id="call_1", ok=True, text="stdout: ok"
    )
    asyncio.run(registry.dispatch(call_event))
    asyncio.run(registry.dispatch(result_event))

    log_path = tmp_path / "home" / "logs" / "audit.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]

    assert records[0]["event"] == "tool_call"
    assert records[0]["tool"] == "bash"
    assert records[0]["args"] == {"command": "ls"}
    assert records[1]["event"] == "tool_result"
    assert records[1]["ok"] is True
    assert records[1]["text"] == "stdout: ok"


def test_notify_done_example_sends_desktop_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / "home"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "notify-send" else None,
    )
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kwargs: calls.append(list(args))
    )
    registry = load_example("notify_done.py")

    event = AgentEndEvent(session_id="abcd1234-5678", usage=LLMUsage())
    asyncio.run(registry.dispatch(event))

    assert calls == [["/usr/bin/notify-send", "QQcode", "Session abcd1234 finished"]]


def test_context_inject_example_returns_injected_message() -> None:
    registry = load_example("context_inject.py")

    event = BeforeRequestEvent(messages=[], model_alias="test-model")
    decision = asyncio.run(registry.dispatch(event))

    assert decision.action == "modify"
    assert len(decision.injected_messages) == 1
    assert "style" in (decision.injected_messages[0].content or "").lower()
