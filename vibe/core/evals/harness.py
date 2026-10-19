from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    CompactionConfig,
    ModelConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.context.overflow import is_context_overflow
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    AgentMode,
    AssistantEvent,
    BaseEvent,
    CompactEndEvent,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    ToolCall,
    ToolResultEvent,
)

EVAL_MODEL_ALIAS = "eval-model"
EVAL_FIXTURES = Path(__file__).parent / "scenarios" / "fixtures"


@dataclass
class ScriptedTurn:
    """One scripted backend response.

    ``tool_calls``/``text`` describe the assistant message; ``exception``
    raises instead (used to simulate provider errors such as context
    overflow). ``finish_reason`` defaults to ``stop`` for the last turn.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    exception: Exception | None = None
    prompt_tokens: int = 10
    completion_tokens: int = 5

    def to_chunk(self) -> LLMChunk:
        message = LLMMessage(
            role=Role.assistant, content=self.text, tool_calls=self.tool_calls or None
        )
        return LLMChunk(
            message=message,
            usage=LLMUsage(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
            ),
            finish_reason=self.finish_reason,
        )


@dataclass
class EvalExpectation:
    """Structural assertions for one scenario (never long prose checks)."""

    max_turns: int | None = None
    required_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    final_contains: list[str] = field(default_factory=list)
    max_context_tokens: int | None = None
    must_compact: bool = False
    must_recover_overflow: bool = False
    custom_check: Callable[[EvalResult], list[str]] | None = None


@dataclass
class EvalScenario:
    """A fixed, network-free agent behaviour scenario."""

    name: str
    user_prompt: str
    fixture_dir: Path
    scripted_turns: list[ScriptedTurn]
    expected: EvalExpectation
    mode: AgentMode = AgentMode.AUTO_APPROVE
    preset_messages: list[LLMMessage] = field(default_factory=list)
    enabled_tools: list[str] | None = None
    tool_paths: list[Path] = field(default_factory=list)
    context_window: int | None = None
    compaction: CompactionConfig | None = None
    prepare: Callable[[Path], None] | None = None
    configure: Callable[[Agent], None] | None = None


@dataclass
class EvalResult:
    """Outcome of running one scenario."""

    scenario: str
    passed: bool
    failures: list[str]
    turns: int
    prompt_tokens: int
    completion_tokens: int
    duration: float
    tools_used: list[str]
    compacted: bool
    recovered_overflow: bool
    final_text: str
    final_messages: list[LLMMessage]
    events: list[BaseEvent]
    workdir: Path


def _result_tools_used(events: Iterable[BaseEvent]) -> list[str]:
    return [
        event.tool_name
        for event in events
        if isinstance(event, ToolResultEvent)
        and not event.skipped
        and event.error is None
    ]


def _errors_from_agent(exc: BaseException) -> list[str]:
    return [f"agent raised {type(exc).__name__}: {exc}"]


def _last_assistant_text(events: list[BaseEvent]) -> str:
    assistant_texts = [
        event.content for event in events if isinstance(event, AssistantEvent)
    ]
    return assistant_texts[-1] if assistant_texts else ""


def _evaluate(scenario: EvalScenario, result: EvalResult) -> list[str]:
    expected = scenario.expected
    failures: list[str] = []

    if expected.required_tools:
        missing = [
            tool for tool in expected.required_tools if tool not in result.tools_used
        ]
        if missing:
            failures.append(f"missing required tools: {missing}")
    if expected.forbidden_tools:
        used = [tool for tool in expected.forbidden_tools if tool in result.tools_used]
        if used:
            failures.append(f"forbidden tools were executed: {used}")
    for needle in expected.final_contains:
        if needle not in result.final_text:
            failures.append(f"final response missing {needle!r}")
    if expected.max_turns is not None and result.turns > expected.max_turns:
        failures.append(f"turns {result.turns} > max {expected.max_turns}")
    if (
        expected.max_context_tokens is not None
        and result.prompt_tokens > expected.max_context_tokens
    ):
        failures.append(
            f"prompt tokens {result.prompt_tokens} > max {expected.max_context_tokens}"
        )
    if expected.must_compact and not result.compacted:
        failures.append("expected compaction, no CompactEndEvent observed")
    if expected.must_recover_overflow and not result.recovered_overflow:
        failures.append("expected context-overflow recovery, none observed")
    if expected.custom_check is not None:
        failures.extend(expected.custom_check(result))
    return failures


def _build_backend(turns: list[ScriptedTurn]) -> FakeBackend:
    chunks: list[LLMChunk] = []
    exceptions: list[Exception | None] = []
    for turn in turns:
        exceptions.append(turn.exception)
        if turn.exception is None:
            chunks.append(turn.to_chunk())
    if chunks:
        chunks[-1] = chunks[-1].model_copy(update={"finish_reason": "stop"})
    return FakeBackend(chunks, exceptions=exceptions)


def _build_config(scenario: EvalScenario, workdir: Path) -> VibeConfig:
    tools = scenario.enabled_tools or [
        call.function.name
        for turn in scenario.scripted_turns
        for call in turn.tool_calls
        if call.function.name
    ]
    # Always activate the keyless local model: the default active model would
    # otherwise require an API key (the harness never calls a provider).
    model = ModelConfig(
        name="eval-model",
        provider="llamacpp",
        alias=EVAL_MODEL_ALIAS,
        context_window=scenario.context_window or 200_000,
    )
    overrides: dict[str, Any] = {"models": [model], "active_model": EVAL_MODEL_ALIAS}
    if scenario.compaction is not None:
        overrides["compaction"] = scenario.compaction

    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
        include_context_files=False,
        workdir=workdir,
        tool_paths=[str(path) for path in scenario.tool_paths],
        enabled_tools=list(dict.fromkeys(tools)),
        tools={
            name: BaseToolConfig(permission=ToolPermission.ALWAYS) for name in tools
        },
        **overrides,
    )


async def run_scenario(
    scenario: EvalScenario, *, keep_workdir: bool = False
) -> EvalResult:
    """Run ``scenario`` against a scripted :class:`FakeBackend` (no network)."""
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="qqcode-eval-") as tmp:
        workdir = Path(tmp) / "workspace"
        workdir.mkdir(parents=True, exist_ok=True)
        if scenario.fixture_dir.is_dir():
            shutil.copytree(scenario.fixture_dir, workdir, dirs_exist_ok=True)
        if scenario.prepare is not None:
            scenario.prepare(workdir)

        backend = _build_backend(scenario.scripted_turns)
        config = _build_config(scenario, workdir)
        agent = Agent(
            config,
            mode=scenario.mode,
            backend=backend,
            auto_approve=scenario.mode == AgentMode.AUTO_APPROVE,
        )
        for message in scenario.preset_messages:
            agent.add_message(message)
        if scenario.configure is not None:
            scenario.configure(agent)

        events: list[BaseEvent] = []
        failure_messages: list[str] = []
        try:
            async for event in agent.act(scenario.user_prompt):
                events.append(event)
        except Exception as exc:  # scripts may intentionally fail the agent
            failure_messages.extend(_errors_from_agent(exc))

        compacted = any(isinstance(event, CompactEndEvent) for event in events)
        scripted_overflow = any(
            turn.exception is not None and is_context_overflow(turn.exception)
            for turn in scenario.scripted_turns
        )
        final_text = _last_assistant_text(events)
        result = EvalResult(
            scenario=scenario.name,
            passed=False,
            failures=failure_messages,
            turns=len(backend.requests_messages),
            prompt_tokens=agent.stats.session_prompt_tokens,
            completion_tokens=agent.stats.session_completion_tokens,
            duration=time.perf_counter() - start,
            tools_used=_result_tools_used(events),
            compacted=compacted,
            recovered_overflow=compacted and scripted_overflow and bool(final_text),
            final_text=final_text,
            final_messages=list(agent.messages),
            events=events,
            workdir=workdir,
        )
        result.failures.extend(_evaluate(scenario, result))
        result.passed = not result.failures

        if keep_workdir:
            kept = Path(tempfile.mkdtemp(prefix="qqcode-eval-kept-")) / "workspace"
            shutil.copytree(workdir, kept, dirs_exist_ok=True)
            result.workdir = kept
        return result


def run_scenarios(
    scenarios: Iterable[EvalScenario], *, keep_workdirs: bool = False
) -> list[EvalResult]:
    """Run scenarios sequentially, each in its own event loop."""

    async def _run_all() -> list[EvalResult]:
        return [
            await run_scenario(scenario, keep_workdir=keep_workdirs)
            for scenario in scenarios
        ]

    return asyncio.run(_run_all())
