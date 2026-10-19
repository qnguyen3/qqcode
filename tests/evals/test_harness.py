from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from vibe.core.evals.__main__ import main as evals_main
from vibe.core.evals.harness import (
    EvalExpectation,
    EvalScenario,
    ScriptedTurn,
    run_scenario,
)
from vibe.core.evals.scenarios import (
    all_scenarios,
    compact_keeps_recent,
    hook_gate,
    overflow_recovery,
    parallel_tools,
    plan_mode_readonly,
    truncation_notice,
)

EXPECTED_SCENARIOS = {
    "compact_keeps_recent",
    "overflow_recovery",
    "parallel_tools",
    "plan_mode_readonly",
    "truncation_notice",
    "hook_gate",
}


@pytest.fixture(autouse=True)
def _isolate_qqcode_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / "qqcode-home"))


def test_six_baseline_scenarios_are_declared() -> None:
    scenarios = all_scenarios()

    assert {scenario.name for scenario in scenarios} == EXPECTED_SCENARIOS
    assert len(scenarios) == 6
    assert all(scenario.fixture_dir.is_dir() for scenario in scenarios)


def test_scenario_constructors_match_registry() -> None:
    constructors = [
        compact_keeps_recent,
        overflow_recovery,
        parallel_tools,
        plan_mode_readonly,
        truncation_notice,
        hook_gate,
    ]

    assert {constructor().name for constructor in constructors} == EXPECTED_SCENARIOS


@pytest.mark.asyncio
async def test_harness_runs_every_scenario_offline() -> None:
    results = [await run_scenario(scenario) for scenario in all_scenarios()]

    failures = {
        result.scenario: result.failures for result in results if not result.passed
    }
    assert failures == {}
    assert all(result.turns >= 1 for result in results)
    assert all(result.duration >= 0 for result in results)


@pytest.mark.asyncio
async def test_harness_reports_expectation_failures() -> None:
    scenario = EvalScenario(
        name="expectation_failures",
        user_prompt="hello",
        fixture_dir=Path("/nonexistent-fixture"),
        scripted_turns=[ScriptedTurn(text="all good")],
        expected=EvalExpectation(
            required_tools=["missing_tool"],
            final_contains=["absent"],
            max_turns=0,
            must_compact=True,
        ),
    )

    result = await run_scenario(scenario)

    assert result.passed is False
    assert any("missing required tools" in failure for failure in result.failures)
    assert any("final response missing" in failure for failure in result.failures)
    assert any("turns 1 > max 0" in failure for failure in result.failures)
    assert any("expected compaction" in failure for failure in result.failures)
    assert result.final_text == "all good"


@pytest.mark.asyncio
async def test_harness_reports_forbidden_tool_use() -> None:
    scenario = replace(
        truncation_notice(),
        name="forbidden_bash",
        expected=EvalExpectation(forbidden_tools=["bash"]),
    )

    result = await run_scenario(scenario)

    assert result.passed is False
    assert any(
        "forbidden tools were executed" in failure for failure in result.failures
    )


def test_cli_runs_all_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = evals_main(["--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {entry["scenario"] for entry in payload} == EXPECTED_SCENARIOS
    assert all(entry["passed"] for entry in payload)


def test_cli_rejects_unknown_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = evals_main(["does_not_exist"])

    assert exit_code == 2
    assert "Unknown scenarios" in capsys.readouterr().err
