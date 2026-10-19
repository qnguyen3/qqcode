# Hooks

QQcode hooks let extensions observe and influence the agent lifecycle without
modifying core code. A hook is a Python module that registers handlers for
lifecycle events; the agent dispatches events at fixed points in the turn loop
and merges handler decisions.

## Event types

| Event | When | Decision honored |
|---|---|---|
| `agent_start` | Start of `Agent.act()` | Injected messages are appended to the conversation |
| `before_request` | Before every LLM request | `deny` stops the turn; injected messages are added to the request |
| `after_response` | After every LLM response | Observe only |
| `tool_call` | Before a tool is invoked | `deny` skips the tool call; `modify` replaces args |
| `tool_result` | After a tool invocation | Observe only |
| `agent_end` | When the turn loop finishes | Observe only |

Event models live in `vibe/core/hooks/events.py`. Every event carries `type`
and an ISO-8601 `timestamp`.

## Hook locations

Hook modules are loaded at agent startup from, in priority order:

1. Directories listed in the `hooks.paths` config key.
2. `./.qqcode/hooks/*.py` (project; searched upward from the work directory).
3. Package `hooks` resource directories (see [packages](packages.md)).
4. `~/.qqcode/hooks/*.py` (global).

> **Security:** hooks execute arbitrary Python code. Project hooks are code
> committed to a repository. Trust gating (plan 10) is not implemented yet:
> project hooks are currently loaded unconditionally, so only check out
> repositories you trust.

## Configuration

```toml
[hooks]
enabled = true
timeout = 5.0
paths = ["/absolute/or/relative/hooks/dir"]
```

`timeout` is the per-handler limit in seconds. `enabled = false` disables all
hooks and is the recommended rollback switch.

## Writing a hook

A hook module can export `def register(registry) -> None`:

```python
from vibe.core.hooks.registry import HookRegistry


def on_tool_call(event) -> None:
    print("tool about to run:", event.tool_name)


def register(registry: HookRegistry) -> None:
    registry.register("tool_call", on_tool_call, priority=0)
```

Alternatively, decorate handlers with the convenience API; when a module has no
`register` function, decorated bindings are auto-discovered:

```python
from vibe.core.hooks.api import deny, on_tool_call


@on_tool_call("bash")
def guard(event):
    if "rm -rf" in event.args.get("command", ""):
        return deny("destructive command blocked")
```

Handlers may be sync functions, `async def` functions, or lambdas. They run by
descending `priority` and then registration order. A handler that raises, times
out, or returns an unsupported value is logged and skipped; it can never crash
the agent.

## Decisions

Handlers return a `HookDecision` (or `None` for "no opinion"):

```python
HookDecision(
    action="allow" | "deny" | "modify",
    reason=None,                  # human-readable explanation
    replacement_args=None,        # tool_call: full replacement args dict
    injected_messages=[],         # agent_start / before_request: extra messages
)
```

Merging rules when several handlers run:

- `deny` wins over `modify` and `allow`.
- `replacement_args` are merged key by key; later handlers win.
- `injected_messages` are appended in handler order.

`agent_start`, `before_request` and `tool_call` decisions are acted on by the
agent. The observe-only events (`after_response`, `tool_result`, `agent_end`)
still run their handlers, but dispatch always returns `allow` for them.

For `tool_call`, replacement args are re-validated against the tool's args
model before execution. Invalid replacement args fail the tool call with a
`tool_error` result instead of invoking the tool.

## Helper API

`vibe.core.hooks.api` provides:

```python
from vibe.core.hooks.api import on_tool_call, deny, modify, inject, on_event

@on_tool_call()                       # any tool
def audit(event): ...

@on_tool_call("bash", priority=10)    # one tool, higher priority
def guard(event): ...

deny("reason")                        # block request/tool call
modify(replacement_args={"a": 1})     # replace tool args
inject("style reminder")              # add a user message to the request
on_event("agent_end")                 # bind to any event type
```

`inject()` is a `modify` decision with a single injected message. Injected
`before_request` messages are transient: they are added to the outgoing request
but not persisted to the session history. Messages injected by `agent_start`
are persisted once at the beginning of the turn.

## Agent integration

- `tool_call` hooks run sequentially, in call order, during the assessment /
  approval phase, before permission checks and before any parallel execution.
  A modified `tool_call` therefore flows into approval and execution with the
  new arguments.
- `before_request` `deny` terminates the turn with an assistant message
  `Request blocked by hook: <reason>`; the backend is never called.
- Hooks run for both streaming and non-streaming turns.

## Examples

Runnable examples live in `examples/hooks/`:

| Example | Purpose |
|---|---|
| `protected_paths.py` | Deny `write_file`/`edit` to `.env`, `secrets/**`, keys |
| `audit_log.py` | Append every tool call/result to `~/.qqcode/logs/audit.jsonl` |
| `notify_done.py` | Desktop notification (`notify-send`/`osascript`) on `agent_end` |
| `context_inject.py` | Inject a style reminder on every `before_request` |

Install one by copying it to `~/.qqcode/hooks/` (global) or
`<project>/.qqcode/hooks/` (project):

```bash
mkdir -p ~/.qqcode/hooks
cp examples/hooks/protected_paths.py ~/.qqcode/hooks/
```
