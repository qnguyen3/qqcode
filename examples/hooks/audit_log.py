"""Append every tool call and tool result to ``~/.qqcode/logs/audit.jsonl``."""

from __future__ import annotations

import json
from pathlib import Path

from vibe.core.config import get_vibe_home
from vibe.core.hooks.events import ToolCallEventHook, ToolResultEventHook
from vibe.core.hooks.registry import HookRegistry


def _audit_log_path() -> Path:
    return get_vibe_home() / "logs" / "audit.jsonl"


def _append(record: dict[str, object]) -> None:
    path = _audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def on_tool_call(event: ToolCallEventHook) -> None:
    _append({
        "event": "tool_call",
        "timestamp": event.timestamp,
        "tool": event.tool_name,
        "tool_call_id": event.tool_call_id,
        "args": event.args,
    })


def on_tool_result(event: ToolResultEventHook) -> None:
    _append({
        "event": "tool_result",
        "timestamp": event.timestamp,
        "tool": event.tool_name,
        "tool_call_id": event.tool_call_id,
        "ok": event.ok,
        "text": event.text,
        "error": event.error,
    })


def register(registry: HookRegistry) -> None:
    registry.register("tool_call", on_tool_call)
    registry.register("tool_result", on_tool_result)
