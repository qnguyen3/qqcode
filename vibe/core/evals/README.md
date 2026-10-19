# Eval harness

Fixed, network-free scenarios that guard agent behaviour against regressions.

## Layout

- `harness.py` — `EvalScenario`, `EvalExpectation`, `ScriptedTurn`, `EvalResult`
  and `run_scenario`. Backends are scripted with `tests/stubs/fake_backend.py`;
  no HTTP requests are made.
- `scenarios/__init__.py` — the six baseline scenarios and `all_scenarios()`.
- `scenarios/fixtures/<scenario>/` — workdir seeds. Each run copies the
  fixture into a temporary directory, so scenarios never mutate the repo.
- `__main__.py` — `qqcode-evals` entry point (Rich table or `--json`).

## Scenarios

| Scenario | Checks |
|---|---|
| `compact_keeps_recent` | compaction happens and the latest exchange survives |
| `overflow_recovery` | a context-length error triggers compaction + retry |
| `parallel_tools` | two independent tool calls overlap (probe log ordering) |
| `plan_mode_readonly` | write tools are skipped in plan mode |
| `truncation_notice` | large bash output gets a notice and a spill file |
| `hook_gate` | a `tool_call` hook blocks a dangerous command |

Assertions are structural: tools used, turn counts, event types, short
`final_contains` fragments and scenario-specific `custom_check` callbacks.
Never assert on long prose.

## Running

```bash
uv run pytest tests/evals -q      # part of the normal suite (< 10s total)
uv run qqcode-evals               # same scenarios, Rich table
uv run qqcode-evals hook_gate     # subset
uv run qqcode-evals --json        # machine-readable results
```
