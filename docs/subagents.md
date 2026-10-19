# Subagents (`task` tool)

The `task` tool delegates an independent job to a **subagent**: a child agent
with its own context window, its own session file and its own budget. The parent
only receives the child's final text plus usage, so intermediate tool calls do
not pollute the parent's context.

## Configuration

```toml
[subagent]
enabled = true          # false hides the task tool entirely
timeout = 600.0         # hard wall-clock limit per subagent (10 minutes)
max_concurrent = 2      # simultaneous subagent runs; extra calls get an error
# max_price = 0.50      # optional per-subagent cost cap in dollars (omit to disable)
stream_tools = false    # forward child tool events to the parent UI ([sub] prefix)
default_agent = "general"  # agent config used when the model does not name one
```

Setting `enabled = false` is the rollback switch: the `task` tool disappears
from the parent's tool list, without affecting any other flow.

## Agent configs

The child starts from an agent config under `~/.qqcode/agents/<name>.toml` (or a
project `.qqcode/agents/<name>.toml`, or a package-provided agent). The model can
pick one per call with `task(agent="...")`; otherwise `subagent.default_agent` is
used. If the config is missing, the runner falls back to a restricted copy of the
parent's configuration.

```toml
# ~/.qqcode/agents/general.toml
system_prompt_id = "cli"
disabled_tools = ["task", "skill"]
```

See `examples/agents/` for ready-to-copy examples.

## Hard limits

Every child run is constrained, regardless of the agent config:

- **No recursion**: `task` (and `skill`) are always removed from the child
  toolset, so a subagent can never spawn another subagent.
- **Read-only by default**: when the agent config does not select tools, the
  child gets `read_file`, `grep`, `find` and `ls` only. An explicit
  `enabled_tools`/`disabled_tools` list may broaden this, but `task`/`skill`
  stay blocked.
- **Path sandbox**: file tools enforce a `path_allowlist` covering the parent's
  working directory; paths outside it are denied even in auto-approve mode.
- **Auto-approve**: the child runs in `AUTO_APPROVE` mode inside that sandbox,
  so it never blocks waiting for interactive approval.
- **Budget**: `max_turns` (default 15, per call), optional `subagent.max_price`,
  and a hard `subagent.timeout` (default 10 minutes).
- **Concurrency**: at most `subagent.max_concurrent` runs execute at once; the
  extra calls return `too many concurrent subagents` so the model can wait.

Aborting the parent cancels the running child and flushes its session.

## Sessions

The child session is written to the same project slug directory as the parent,
as JSONL. Its header carries `parent_session_id` pointing at the parent session.
The parent's conversation history only gains the tool result message; a
`subagent_result` custom entry is recorded on both sessions for `/session`
statistics.

## UI

Tool calls render as `task: <description>`. While a child runs, summary
`SubagentEvent`s update the loading status (description + turn count); on
completion the result line shows `Completed: N turns, M tokens`, and failures
render the error. With `stream_tools = true`, child tool events are forwarded
with a `[sub]` prefix.
