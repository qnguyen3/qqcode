"""Scripted eval scenarios.

Layout (documented in ``vibe/core/evals/README.md``):

- ``vibe/core/evals/harness.py`` — scenario runner built on ``FakeBackend``.
- ``vibe/core/evals/scenarios/__init__.py`` — scenario definitions.
- ``vibe/core/evals/scenarios/fixtures/<name>/`` — per-scenario workdir seeds
  (copied to a temporary directory before each run; never written in place).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vibe.core.agent import Agent
from vibe.core.config import CompactionConfig
from vibe.core.evals.harness import (
    EVAL_FIXTURES,
    EvalExpectation,
    EvalResult,
    EvalScenario,
    ScriptedTurn,
)
from vibe.core.hooks.api import deny
from vibe.core.hooks.events import HookDecision, ToolCallEventHook
from vibe.core.types import (
    AgentMode,
    FunctionCall,
    LLMMessage,
    Role,
    ToolCall,
    ToolResultEvent,
)

FIXTURES = EVAL_FIXTURES
_PROBE_LOG_LINES = 4


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments))
    )


def _fixture(name: str) -> Path:
    return FIXTURES / name


def _tool_results(result: EvalResult, tool_name: str) -> list[ToolResultEvent]:
    return [
        event
        for event in result.events
        if isinstance(event, ToolResultEvent) and event.tool_name == tool_name
    ]


def compact_keeps_recent() -> EvalScenario:
    """Long history compacts, but the most recent exchange survives intact."""

    def check(result: EvalResult) -> list[str]:
        contents = [message.content or "" for message in result.final_messages]
        failures: list[str] = []
        if not any("RECENT_MARKER" in content for content in contents):
            failures.append("recent user message was dropped by compaction")
        if any("old question" in content for content in contents):
            failures.append("old user message survived compaction")
        return failures

    return EvalScenario(
        name="compact_keeps_recent",
        user_prompt="Hello",
        fixture_dir=_fixture("compact_keeps_recent"),
        mode=AgentMode.AUTO_APPROVE,
        context_window=1,
        compaction=CompactionConfig(reserve_tokens=0, keep_recent_tokens=150),
        preset_messages=[
            LLMMessage(role=Role.user, content="old question " * 80),
            LLMMessage(role=Role.assistant, content="old answer " * 80),
            LLMMessage(role=Role.user, content="recent question RECENT_MARKER " * 20),
            LLMMessage(role=Role.assistant, content="recent answer " * 20),
        ],
        scripted_turns=[ScriptedTurn(text="<summary>"), ScriptedTurn(text="<final>")],
        expected=EvalExpectation(
            must_compact=True, final_contains=["<final>"], custom_check=check
        ),
    )


def overflow_recovery() -> EvalScenario:
    """Provider context overflow triggers compaction and a successful retry."""
    return EvalScenario(
        name="overflow_recovery",
        user_prompt="Summarize the project.",
        fixture_dir=_fixture("overflow_recovery"),
        mode=AgentMode.AUTO_APPROVE,
        context_window=200_000,
        scripted_turns=[
            ScriptedTurn(
                exception=RuntimeError(
                    "This model's maximum context length is 8192 tokens"
                )
            ),
            ScriptedTurn(text="<summary>"),
            ScriptedTurn(text="<final>"),
        ],
        expected=EvalExpectation(
            must_compact=True, must_recover_overflow=True, final_contains=["<final>"]
        ),
    )


def parallel_tools() -> EvalScenario:
    """Two independent tool calls in one turn overlap instead of serializing."""

    def check(result: EvalResult) -> list[str]:
        log_path = result.workdir / "probe.log"
        if not log_path.is_file():
            return ["concurrency probe never ran"]
        lines = [
            line.strip()
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(lines) != _PROBE_LOG_LINES:
            return [f"expected 4 probe log lines, got {len(lines)}"]
        if not lines[0].startswith("start:") or not lines[1].startswith("start:"):
            return [f"tool calls did not overlap: {lines}"]
        if not all(line.startswith("end:") for line in lines[2:]):
            return [f"unexpected probe log ordering: {lines}"]
        return []

    return EvalScenario(
        name="parallel_tools",
        user_prompt="Probe concurrency.",
        fixture_dir=_fixture("parallel_tools"),
        mode=AgentMode.AUTO_APPROVE,
        enabled_tools=["concurrency_probe"],
        tool_paths=[_fixture("parallel_tools") / "tools"],
        scripted_turns=[
            ScriptedTurn(
                tool_calls=[
                    _call("probe_a", "concurrency_probe", {"label": "a"}),
                    _call("probe_b", "concurrency_probe", {"label": "b"}),
                ]
            ),
            ScriptedTurn(text="probe done"),
        ],
        expected=EvalExpectation(
            required_tools=["concurrency_probe"],
            final_contains=["probe done"],
            custom_check=check,
        ),
    )


def plan_mode_readonly() -> EvalScenario:
    """Plan mode refuses write tools and leaves the filesystem untouched."""

    def check(result: EvalResult) -> list[str]:
        failures: list[str] = []
        results = _tool_results(result, "write_file")
        if not results:
            failures.append("write_file call was not reported")
        elif not results[0].skipped:
            failures.append("write_file was executed in plan mode")
        elif "Plan mode" not in (results[0].skip_reason or ""):
            failures.append("skip reason does not mention plan mode")
        if (result.workdir / "plan.txt").exists():
            failures.append("plan mode still wrote plan.txt")
        return failures

    return EvalScenario(
        name="plan_mode_readonly",
        user_prompt="Write a file with my plan.",
        fixture_dir=_fixture("plan_mode_readonly"),
        mode=AgentMode.PLAN,
        enabled_tools=["write_file"],
        scripted_turns=[
            ScriptedTurn(
                tool_calls=[
                    _call(
                        "plan_write",
                        "write_file",
                        {"path": "plan.txt", "content": "should not exist"},
                    )
                ]
            ),
            ScriptedTurn(text="plan only"),
        ],
        expected=EvalExpectation(
            forbidden_tools=["write_file"],
            final_contains=["plan only"],
            custom_check=check,
        ),
    )


def truncation_notice() -> EvalScenario:
    """Large bash output is truncated with a notice and a temp spill file."""

    def prepare(workdir: Path) -> None:
        (workdir / "big.txt").write_text("x" * 200_000, encoding="utf-8")

    def check(result: EvalResult) -> list[str]:
        failures: list[str] = []
        results = _tool_results(result, "bash")
        if not results:
            return ["bash call was not reported"]
        payload = results[0].result
        if payload is None:
            return ["bash result missing"]
        if not getattr(payload, "truncated", False):
            failures.append("large output was not truncated")
        if not getattr(payload, "full_output_path", None):
            failures.append("truncated output has no spill file")
        if not getattr(payload, "notice", None):
            failures.append("truncation notice missing")
        return failures

    return EvalScenario(
        name="truncation_notice",
        user_prompt="Print the big file.",
        fixture_dir=_fixture("truncation_notice"),
        mode=AgentMode.AUTO_APPROVE,
        enabled_tools=["bash"],
        prepare=prepare,
        scripted_turns=[
            ScriptedTurn(
                tool_calls=[_call("big_out", "bash", {"command": "cat big.txt"})]
            ),
            ScriptedTurn(text="truncated"),
        ],
        expected=EvalExpectation(
            required_tools=["bash"], final_contains=["truncated"], custom_check=check
        ),
    )


def hook_gate() -> EvalScenario:
    """A tool_call hook blocks a dangerous bash command before execution."""

    def register(agent: Agent) -> None:
        def guard(event: ToolCallEventHook) -> HookDecision | None:
            if event.tool_name == "bash" and "touch" in event.args.get("command", ""):
                return deny("destructive command blocked")
            return None

        agent.hooks.register("tool_call", guard)

    def check(result: EvalResult) -> list[str]:
        failures: list[str] = []
        results = _tool_results(result, "bash")
        if not results:
            return ["bash call was not reported"]
        if not results[0].skipped:
            failures.append("hook did not block the bash call")
        if results[0].skip_reason != "destructive command blocked":
            failures.append(f"unexpected skip reason: {results[0].skip_reason!r}")
        if (result.workdir / "canary.txt").exists():
            failures.append("blocked command still ran")
        return failures

    return EvalScenario(
        name="hook_gate",
        user_prompt="Clean up temporary files.",
        fixture_dir=_fixture("hook_gate"),
        mode=AgentMode.AUTO_APPROVE,
        enabled_tools=["bash"],
        configure=register,
        scripted_turns=[
            ScriptedTurn(
                tool_calls=[_call("danger", "bash", {"command": "touch canary.txt"})]
            ),
            ScriptedTurn(text="blocked by hook"),
        ],
        expected=EvalExpectation(
            forbidden_tools=["bash"], final_contains=["blocked"], custom_check=check
        ),
    )


def all_scenarios() -> list[EvalScenario]:
    """Return the six baseline scenarios in a stable order."""
    return [
        compact_keeps_recent(),
        overflow_recovery(),
        parallel_tools(),
        plan_mode_readonly(),
        truncation_notice(),
        hook_gate(),
    ]
