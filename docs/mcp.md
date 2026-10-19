# MCP in QQcode

QQcode speaks the [Model Context Protocol](https://modelcontextprotocol.io) and
can connect to stdio and HTTP MCP servers. Server connections are persistent,
tools and resources are discovered once, and the tool list is refreshed when a
server reports `notifications/tools/list_changed`.

## Configuration

MCP servers live in `mcp_servers` in your config file
(`~/.qqcode/config.toml`, or `.qqcode/config.toml` in a project):

```toml
[[mcp_servers]]
name = "fetch_server"          # short alias used to prefix tool names
transport = "stdio"
command = "uvx"
args = ["mcp-server-fetch"]
enabled = true                 # default: true
timeout = 60.0                 # per-operation timeout in seconds
tool_exposure = "direct"       # direct | deferred | hidden
env = { API_TOKEN = "..." }    # optional extra environment variables

[[mcp_servers]]
name = "my_http_server"
transport = "streamable-http"
url = "http://localhost:8001"
headers = { "X-API-Key" = "..." }
# api_key_env = "MY_API_KEY_ENV_VAR"   # read a bearer token from the environment
# api_key_header = "Authorization"
# api_key_format = "Bearer {token}"

# OAuth 2.1 (dynamic client registration + PKCE by default)
[mcp_servers.oauth]
# client_id = "..."          # skip DCR with a pre-registered client
# client_secret = "..."
# callback_port = 3334       # fixed loopback port for the redirect
# auth_server_url = "..."    # override discovery
scopes = ["read"]
```

Field reference:

| Field | Description |
| --- | --- |
| `name` | Alias used to prefix tool names (`{name}_{tool}`) and the `mcp__{name}__*` resource tools. |
| `transport` | `stdio`, `http`, or `streamable-http`. |
| `enabled` | When `false`, the server is not connected (rollback switch). |
| `timeout` | Timeout for discovery and tool calls. |
| `tool_exposure` | `direct` (default), `deferred`, or `hidden` (see below). |
| `prompt` | Optional usage hint appended to tool descriptions. |
| `env` | Extra environment variables for stdio servers. |
| `oauth` | OAuth configuration for protected HTTP servers. |

Old configs without `enabled`/`timeout`/`tool_exposure`/`oauth` keep working
with the defaults shown above.

## CLI

```bash
qqcode mcp list [--json]                  # status, transport, number of tools
qqcode mcp add <name> --url URL [--oauth] [--header K=V] [--env K=V] [--timeout S]
qqcode mcp add <name> -- cmd args...      # stdio server
qqcode mcp remove <name> [--local]
qqcode mcp login <name> [--no-browser]
qqcode mcp logout <name>
```

`add` connects to the server and lists its tools before writing the config
(fail early); pass `--no-verify` for offline/CI environments. `--local` writes
to `.qqcode/config.toml` in the current directory instead of the global config.

## Tool exposure

Long MCP tool lists can crowd the prompt. `tool_exposure` controls what the
model sees:

- `direct` (default): every server tool and resource tool is in the tool list.
- `hidden`: nothing from the server is exposed; tools remain callable by their
  full name (for example from hooks or custom code).
- `deferred`: tools are hidden by default and an `mcp_search` tool is added.
  `mcp_search {query}` runs a dependency-free BM25 search over tool names and
  descriptions, returns the top matches, and activates them for the rest of the
  current turn. Activation is one-shot: the tools are hidden again when the
  next turn starts.

Resource tools are always exposed with `direct` servers:

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `mcp__<server>__list_resources` | – | List resource URIs, names and MIME types. |
| `mcp__<server>__list_resource_templates` | – | List templated resource URIs. |
| `mcp__<server>__read_resource` | `uri` | Read a resource. |

Resource tools are read-only and default to the `ask` permission. Text output
uses the standard truncation rules (2 000 lines / 50 KiB) and spills the full
content to a temporary file; binary content is written to a temporary file and
reported with its size.

## OAuth

HTTP servers that return `401`/`403` can be authenticated with OAuth 2.1.
Configure the `oauth` block, then run:

```bash
qqcode mcp login my_http_server
```

QQcode performs metadata discovery, dynamic client registration (unless
`client_id` is set), PKCE, and waits on a loopback callback
(`--no-browser` prints the URL instead of opening a browser). Tokens are stored
in `~/.qqcode/mcp-auth.json` with `chmod 600`, keyed by server name and URL.
Expired access tokens are refreshed automatically on the next request; if
refresh fails (or no token exists) tool calls fail with a message telling you
to run `qqcode mcp login <server>`. `qqcode mcp logout <server>` removes the
stored credentials.

## Lifecycle

One `ClientSession` is kept open per server for the lifetime of the agent
session. A dedicated runner task owns the connection, so stdio and HTTP
transports are entered and exited in the same task. On a dropped session the
client reconnects once and retries the call; failed connections are retried
with exponential backoff. The interactive UI and programmatic/ACP entry points
start the manager before the agent runs and stop it on exit.

## Permissions and naming

MCP tools keep the historical names (`{server}_{tool}`) and can be configured
like any other tool:

```toml
[tools.fetch_server_fetch]
permission = "always"
```
