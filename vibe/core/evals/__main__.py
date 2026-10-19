from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from vibe.core.evals.harness import EvalResult, run_scenarios
from vibe.core.evals.scenarios import all_scenarios


def _format_result(result: EvalResult) -> str:
    return "PASS" if result.passed else "FAIL"


def main(argv: list[str] | None = None) -> int:
    """Run the eval scenarios; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="qqcode-evals",
        description="Run scripted qqcode eval scenarios (no network).",
    )
    parser.add_argument(
        "names",
        nargs="*",
        metavar="SCENARIO",
        help="Scenario names to run (defaults to every scenario).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON results.")
    args = parser.parse_args(argv)

    scenarios = all_scenarios()
    if args.names:
        wanted = set(args.names)
        known = {scenario.name for scenario in scenarios}
        unknown = wanted - known
        if unknown:
            print(f"Unknown scenarios: {sorted(unknown)}", file=sys.stderr)
            return 2
        scenarios = [scenario for scenario in scenarios if scenario.name in wanted]

    results = run_scenarios(scenarios)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "scenario": result.scenario,
                        "passed": result.passed,
                        "failures": result.failures,
                        "turns": result.turns,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "duration": round(result.duration, 4),
                    }
                    for result in results
                ],
                indent=2,
            )
        )
    else:
        console = Console()
        table = Table(title="qqcode evals")
        table.add_column("scenario")
        table.add_column("result")
        table.add_column("turns", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("duration", justify="right")
        for result in results:
            table.add_row(
                result.scenario,
                _format_result(result),
                str(result.turns),
                str(result.prompt_tokens + result.completion_tokens),
                f"{result.duration:.3f}s",
            )
        console.print(table)
        for result in results:
            for failure in result.failures:
                console.print(f"[red]{result.scenario}: {failure}[/red]")

    return 0 if all(result.passed for result in results) else 1


def cli() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
