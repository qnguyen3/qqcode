# QQcode Python Embedding SDK (`vibe.sdk`)

`vibe.sdk` provides a stable, typed async Python API for embedding the QQcode
agent directly into another Python application without shelling out to the CLI.

- **API version:** `vibe.sdk.__api_version__ == 1`
- **Event contract:** Shared with `qqcode -p ... --output events` (`schema == 1`)

## Quick start

```python
import asyncio
from vibe.sdk import AgentOptions, MessageDeltaEvent, TurnCompletedEvent, create_agent


async def main() -> None:
    options = AgentOptions(
        model_alias="devstral-latest",
        auto_approve=True,
    )
    async with create_agent(options) as session:
        async for event in session.prompt("Summarize README.md"):
            match event:
                case MessageDeltaEvent(delta=chunk):
                    print(chunk, end="", flush=True)
                case TurnCompletedEvent(usage=usage, cost_usd=cost):
                    print(f"\n({usage.input} in / {usage.output} out, ${cost:.4f})")


asyncio.run(main())
```

## Public surface

```python
from vibe.sdk import (
    __api_version__,
    AgentOptions,
    AgentSession,
    SDKEvent,
    SessionRef,
    create_agent,
    sdk_event_json_schema,
)
```

### `AgentOptions`

| Field | Type | Default | Description |
|---|---|---|---|
| `config_path` | `Path \| None` | `None` | Custom `config.toml` file path. |
| `agent` | `str \| None` | `None` | Named agent profile under `~/.qqcode/agents/<name>.toml`. |
| `model_alias` | `str \| None` | `None` | Override the active model alias. |
| `enabled_tools` | `list[str] \| None` | `None` | Restrict available tools by name or glob. |
| `system_prompt` | `str \| None` | `None` | Replace the system prompt. |
| `append_system_prompt` | `list[str]` | `[]` | Extra instructions appended to the system prompt. |
| `auto_approve` | `bool` | `False` | Approve tool executions automatically. |
| `working_dir` | `Path` | `Path.cwd()` | Working directory for tools and session storage. |
| `save_session` | `bool` | `True` | Persist conversation entries to `.jsonl`. |
| `session` | `SessionRef \| None` | `None` | Resume an existing session or fork at an entry ID. |

### `AgentSession`

- `async with create_agent(options) as session:` manages MCP connections and flushes session state on exit.
- `async for event in session.prompt(text, attachments=[...]):` runs a turn and yields typed `SDKEvent` objects.
- `await session.steer(text)`: queues a follow-up user message mid-turn.
- `await session.abort()`: cancels the active turn.
- `await session.compact(instructions=None)`: compacts history and emits a `SessionCompactedEvent`.
- `await session.set_model(alias)`: switches the active model.
- `await session.set_thinking(level)`: sets reasoning effort (`"off"`, `"minimal"`, `"low"`, `"medium"`, `"high"`, or `"default"`).
- `session.session_id`: current session ID.
- `session.stats()`: copy of `AgentStats`.
- `session.messages()`: current conversation messages derived from the entry log.
- `AgentSession.list_sessions(workdir=None, limit=20)`: lists persisted sessions.

## Resuming and forking sessions

```python
from vibe.sdk import AgentOptions, SessionRef, create_agent

# Resume an existing session by ID (or prefix):
async with create_agent(
    AgentOptions(session=SessionRef(session_id="01J..."))
) as resumed:
    async for event in resumed.prompt("Continue from where we left off"):
        ...

# Fork a session at a specific entry ID:
async with create_agent(
    AgentOptions(
        session=SessionRef(session_id="01J...", fork_at_entry_id="01JENTRY...")
    )
) as forked:
    async for event in forked.prompt("Try an alternative approach"):
        ...
```

## Versioned event schema

All streaming events belong to the discriminated union `vibe.sdk.SDKEvent`
(discriminated by `type`, with `schema_version = 1` / wire key `"schema": 1`).
Generate the machine-readable JSON Schema at any time with:

```python
import json
from vibe.sdk import sdk_event_json_schema

print(json.dumps(sdk_event_json_schema(), indent=2))
```

| Event class | `type` | Payload fields |
|---|---|---|
| `SessionStartedEvent` | `"session.started"` | `session_id` |
| `TurnStartedEvent` | `"turn.started"` | `turn` |
| `MessageDeltaEvent` | `"message.delta"` | `item_id`, `delta`, `content` |
| `ThinkingUpdatedEvent` | `"thinking.updated"` | `item_id`, `delta`, `content` |
| `ToolCallStreamEvent` | `"tool.call"` | `tool_call_id`, `tool_name`, `args` |
| `ToolResultStreamEvent` | `"tool.result"` | `tool_call_id`, `tool_name`, `status`, `text`, `truncated` |
| `TurnCompletedEvent` | `"turn.completed"` | `usage` (`input`, `output`, `cache_read`, `cache_write`), `cost_usd` |
| `SessionCompactedEvent` | `"session.compacted"` | `tokens_before`, `tokens_after` |
| `ToolApprovalRequiredEvent` | `"tool.approval_required"` | `tool_call_id`, `tool_name`, `args` |
| `PlanApprovalRequiredEvent` | `"plan.approval_required"` | `plan` |
| `ErrorStreamEvent` | `"error"` | `message` |
