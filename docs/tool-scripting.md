# Sandboxed Tool Scripting (`run_tools_script`)

`run_tools_script` (codemode) lets the model write a short Python script that
calls multiple read-only tools, filters or aggregates their outputs in memory,
and prints a concise result. This avoids multiple LLM round-trips and keeps raw
intermediate tool payloads out of the conversation context.

## Configuration

Tool scripting is **experimental and disabled by default**. When disabled,
`run_tools_script` is not registered in `ToolManager`.

Enable and tune it in `config.toml` under `[tools.script]`:

```toml
[tools.script]
enabled = false            # experimental; off by default
timeout = 30.0             # per-script wall-clock timeout in seconds
max_calls = 20             # maximum tool invocations per script
max_output_bytes = 16384   # maximum stdout bytes captured from print()
memory_mb = 256            # RLIMIT_AS virtual memory limit in megabytes
```

## Scripting API

A script receives:

- `tools`: a proxy object exposing `tools.<name>(**args)` for every tool in
  `tool_allowlist`.
- Preloaded standard library modules: `json`, `re`, `math`, `statistics`.
- Safe Python builtins (`print`, `len`, `range`, `sorted`, `min`, `max`, `sum`,
  `enumerate`, `zip`, `dict`, `list`, `set`, `str`, `int`, `float`, `bool`, etc.).

```python
paths = [m["path"] for m in tools.grep(pattern="TODO", path="vibe").matches]
todo = sorted({p for p in paths if p.endswith(".py")})
print(f"{len(todo)} files with TODO")
for p in todo[:5]:
    print(p)
```

### Tool allowlist & `scriptable` attribute

- Tools opt in via `scriptable: ClassVar[bool] = True` on their `BaseTool`
  subclass.
- Built-in read-only tools (`read_file`, `grep`, `find`, `ls`) have
  `scriptable = True`.
- Mutating or side-effecting tools (`write_file`, `edit`, `bash`, `task`,
  `run_tools_script`) default to `scriptable = False` and are rejected even if
  listed in `tool_allowlist`.

### Permissions, hooks, and audit logging

Every `tools.<name>(**args)` call inside a script passes through the same
permission, path-restriction, hook, and audit pipeline as a direct tool call:

- Path boundaries (`path_allowlist` / `path_denylist`) and tool denylists are
  always enforced.
- Tools configured with `permission = "ask"` are **not** interactively prompted
  from inside a script; unless the agent is in `auto_approve` mode or the call
  matches an allowlist pattern, the call is denied immediately.
- Every allowed or denied call records an entry in `AuditLogger` and dispatches
  `tool_call` / `tool_result` lifecycle hooks.

## Security Model & Limitations

> **Warning:** `run_tools_script` provides **process-level isolation**, not a
> hardware virtual machine or micro-VM. Keep it disabled in untrusted
> environments where defense against kernel or interpreter escapes is required.

Isolation layers applied to every script run:

1. **Separate subprocess:** executed via `python -I` (`_script_worker.py`) in a
   new process session with a minimal environment and no inherited file
   descriptors.
2. **Restricted builtins & AST validation:** `import`, `__import__`, `open`,
   `eval`, `exec`, `compile`, `getattr`, `globals`, `locals`, and dunder
   attribute access (`__class__`, `__subclasses__`, `__dict__`, etc.) are
   blocked.
3. **No direct filesystem or network access:** `socket` APIs are disabled before
   user code runs, and `open` is unavailable; all I/O must go through `tools.*`.
4. **Resource caps:** `RLIMIT_AS` (`memory_mb`), `max_calls`, `max_output_bytes`,
   and wall-clock `timeout` (which kills the worker process group with
   `SIGKILL`) prevent runaway scripts.
