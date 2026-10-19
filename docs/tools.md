# Tools

Every tool QQcode discovers can be labelled with an **exposure** that decides
whether its schema is sent to the model:

| Exposure | Behavior |
| --- | --- |
| `direct` (default) | Always part of the tool list. |
| `model_only` | Part of the tool list, hidden from UI listings. |
| `deferred` | Hidden until `tool_search` exposes it for the current turn. |
| `hidden` | Never sent to the model; still callable by name from code. |

## Configuring exposure

Override exposure per tool by name or glob in `config.toml`:

```toml
[tools.exposure]
"mcp.serena_*" = "deferred"
"mcp.fs_*" = "deferred"
bash = "direct"
```

The last matching pattern wins. Tools shipped by a package can set the default
with the `exposure` class variable on the tool class.

## tool_search

When at least one tool is deferred, `tool_search` is added to the tool list.
The model calls it with a short query; QQcode ranks the deferred pool with
BM25 over tool names and descriptions, returns the best matches, and exposes
them for the rest of the current turn. At the start of the next turn the
exposed tools are hidden again (one-shot), unless their exposure is `direct`.

## MCP servers

MCP tools follow their server's `tool_exposure` (see
[docs/mcp.md](mcp.md)). In addition, when a single server exposes more than
`tools.defer_when_more_than` tools (default `30`), those tools are deferred
automatically and become discoverable through `tool_search`. Set a large value
or `exposure = "direct"` overrides to keep them always visible.

Deferring tools shrinks the schema sent on every request; the agent logs the
visible/deferred counts at debug level when deferral is active.
