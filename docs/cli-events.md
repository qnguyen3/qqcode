# CLI event stream (`--output events`)

`qqcode -p "<prompt>" --output events` emits a versioned, newline-delimited JSON
(JSONL) event stream on stdout. It is intended for editor plugins, CI harnesses
and other programmatic consumers that need stable event semantics.

The stream is opt-in. The existing `--output vscode` format is unchanged so the
VSCode extension keeps working.

Every line is a JSON object with these common fields:

| Field | Type | Description |
|---|---|---|
| `type` | string | Event type, see the table below. |
| `schema` | integer | Schema version. Currently `1`. |
| `timestamp` | string | ISO-8601 UTC timestamp of the event. |

## Event types

| `type` | Payload fields | Description |
|---|---|---|
| `session.started` | `session_id` | Emitted once when the formatter starts. |
| `turn.started` | `turn` (1-based) | A new assistant turn began. |
| `message.delta` | `item_id`, `delta`, `content` | Incremental assistant text. `delta` is the new chunk, `content` the full text so far. |
| `tool.call` | `tool_call_id`, `tool_name`, `args` | The model requested a tool call. `args` is the parsed argument object. |
| `tool.result` | `tool_call_id`, `tool_name`, `status`, `text`, `truncated` | Tool outcome. `status` is `ok`, `error` or `skipped`. `text` is the serialized result or the error/skip reason. `truncated` is `false` today. |
| `turn.completed` | `usage`, `cost_usd` | The assistant turn finished. `usage` is `{input, output, cache_read, cache_write}`. |
| `session.compacted` | `tokens_before`, `tokens_after` | Conversation history was compacted. |
| `error` | `message` | A fatal or command-level error. |

Tool approval requests and plan approval requests reuse the VSCode event names
(`tool.approval_required`, `plan.approval_required`) because they share the same
callback path.

## Stdin control commands

When running with `--output events` and stdin is piped, `qqcode` reads JSONL
control messages concurrently with the agent loop. Each message is a JSON object
with a `type` field:

```jsonc
{"type":"compact","instructions":"keep the API details"}  // summarize history
{"type":"abort"}                                          // stop the running turn
{"type":"set_model","alias":"claude-sonnet-4"}            // switch model
{"type":"set_thinking","level":"high"}                    // off|minimal|low|medium|high|default
{"type":"steer","message":"also update the tests"}        // append a user message mid-turn
```

Control commands are applied at turn boundaries wherever possible. `steer`
queues a user message that is appended after the current turn, so the agent
continues without waiting for another prompt. `abort` cancels the running turn
and the process finalizes normally.

Approval responses (`{"approved": true, ...}`) are routed separately from control
commands, so both can share stdin.

## Example

```bash
printf '%s\n' '{"type":"steer","message":"focus on Linux"}' | \
  qqcode -p "analyze this repository" --output events --auto-approve
```

Output (abridged):

```jsonl
{"type":"session.started","schema":1,"timestamp":"2026-01-01T00:00:00+00:00","session_id":"..."}
{"type":"turn.started","schema":1,"timestamp":"...","turn":1}
{"type":"message.delta","schema":1,"timestamp":"...","item_id":"msg_ab12cd34","delta":"Here is ","content":"Here is "}
{"type":"turn.completed","schema":1,"timestamp":"...","usage":{"input":1200,"output":42,"cache_read":0,"cache_write":0},"cost_usd":0.0012}
```

## Compatibility

- `schema` increments on breaking payload changes. Consumers should ignore
  unknown fields and unknown event types.
- The stream never mixes human-readable text into stdout.
- Exit codes follow the regular programmatic mode: `0` on success, `1` on
  conversation limits or runtime errors.
