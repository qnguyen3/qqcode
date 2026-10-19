# Security

QQcode executes model-generated tool calls (shell commands, file writes) with
the privileges of the user running it. This document describes the permission
model, project trust, and the audit log. For stronger isolation see
[sandboxing](sandboxing.md).

## Permission model

Every tool has a permission level (`vibe/core/tools/base.py`):

| Permission | Behaviour |
|---|---|
| `always` | Executes without asking. |
| `ask` | Prompts for approval in interactive mode. |
| `never` | Always skipped. |

Defaults are conservative per tool (for example `read_file` is `always`,
`write_file`, `edit` and `bash` are `ask`). Override them in
`~/.qqcode/config.toml`:

```toml
[tools.write_file]
permission = "ask"

[tools.bash]
permission = "never"
```

In plan mode only read-only tools run; write tools and mutating bash commands
are skipped regardless of their permission level.

### Pattern lists

Path-aware tools (`read_file`, `write_file`, `edit`) support two kinds of
patterns:

- `allowlist` / `denylist` — legacy glob patterns matched against the path as
  written (interactive-mode approval shortcuts).
- `path_allowlist` / `path_denylist` — glob patterns matched against the
  **resolved absolute path**. A non-empty `path_allowlist` denies every path
  that does not match; `path_denylist` wins over `path_allowlist`. Path
  restrictions are enforced in **every** mode, including `--auto-approve` and
  plan mode, so they are a hard boundary rather than an approval shortcut.

```toml
[tools.write_file]
permission = "always"
path_allowlist = ["/home/me/project/**"]
path_denylist = ["/home/me/project/.env", "/home/me/project/secrets/**"]
```

The approval UI shows the resolved path, so `..` and absolute paths outside
the working directory are visible before you approve them. Note that write
tools additionally refuse targets outside the working directory.

## Project trust

A checked-out repository can ship a `.qqcode/` directory whose contents
execute code. QQcode therefore refuses to load project-scoped resources until
you trust the project:

| Resource | Gated |
|---|---|
| `.qqcode/config.toml` (project config) | yes |
| `.qqcode/tools/` (custom tool modules) | yes |
| `.qqcode/hooks/` (hook modules) | yes |
| `.qqcode/mcp.toml`, `.qqcode/mcp.json` (MCP servers) | yes |
| `AGENTS.md`, `QQCODE.md`, `.qqcode.md` (context files) | no — inert prompt text |

Trust is decided in this order:

1. `--trust` / `--no-trust` command-line flag.
2. The trust store at `~/.qqcode/trust.toml`.
3. `default_project_trust` in `~/.qqcode/config.toml` (default `false`).
4. Interactive prompt (Textual UI); headless runs never prompt.

In the interactive UI a modal asks *Trust project?* with **Yes**, **No** and
**Always**; **Always** persists the decision. `--no-trust` and headless runs
(`qqcode -p "..."`) never prompt: project resources are skipped, global
configuration is used, and a warning is printed. A stored decision or
`--trust` loads project resources without asking.

```toml
# ~/.qqcode/trust.toml
[projects]
"/home/me/project" = "trusted"   # "trusted" | "untrusted"
```

Trust one project once with `qqcode --trust`, or trust a directory for a
headless run with `qqcode -p "..." --trust`. For CI, prefer `--no-trust` so a
repository can never influence the run.

## Audit log

Enable the append-only JSONL audit log (disabled by default):

```toml
[audit]
enabled = true
log_args = false          # sensitive tools never store raw arguments anyway
log_path = "~/.qqcode/logs/audit.jsonl"
max_bytes = 5242880       # rotate at 5 MB
keep_files = 3            # audit.jsonl + .1 + .2
```

Each tool permission decision is one line:

```json
{"ts": "2026-01-01T12:00:00+0000", "session_id": "...", "tool": "bash",
 "args_hash": "9f2c…", "decision": "approved", "mode": "interactive",
 "user": "me", "args_summary": "git status"}
```

- `decision` is `approved`, `denied` or `denylist`.
- With `log_args = false` (default) full arguments are never written.
  Sensitive tools (`bash`, `read_file`, `write_file`, `edit`) add a truncated
  `args_summary` (command or path) plus the arguments hash; other tools store
  only the hash.
- Set `log_args = true` only when you accept that commands and file contents
  may appear in the log.
- Rotation is size-based: the active file is renamed to `.1`, older backups
  shift down, and only `keep_files` files are retained.

## Diagnostics

Both are opt-in and stay on the local machine (no network traffic):

```toml
[debug]
enabled = true            # ~/.qqcode/logs/debug.jsonl: request metadata only

[telemetry]
mode = "local"            # off | local; counters in ~/.qqcode/logs/metrics.json
```

The debug log records model, message count, estimated tokens and duration —
never message contents. Local metrics count turns, compactions, context
overflows and tool failures for your own dashboards.

## Best practices

- Run `--no-trust` in CI and never make `default_project_trust = true` a
  machine-wide habit.
- Keep a `path_denylist` for secrets (`.env`, keys, credentials) even inside a
  trusted project.
- Use `--tools` / `--exclude-tools` to expose only the tools a task needs.
- Review `~/.qqcode/logs/audit.jsonl` in shared or automated environments.
- Treat trusted repositories as code you would run yourself: hooks, custom
  tools and MCP servers execute arbitrary code.
- For untrusted or unattended work, use a container or restricted user as
  described in [sandboxing](sandboxing.md).
