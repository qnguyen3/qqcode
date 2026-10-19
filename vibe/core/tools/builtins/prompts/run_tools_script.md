# Sandboxed tool scripting (`run_tools_script`)

Use `run_tools_script` when you need to call multiple read-only tools (`grep`,
`read_file`, `find`, `ls`), filter or aggregate their results in Python, and
return a compact summary in a single step instead of chaining many LLM turns.

## API inside the script

- Declare every tool the script will call in `tool_allowlist` (for example,
  `["grep", "read_file"]`). Only tools marked `scriptable = True` (read-only
  tools by default) are permitted.
- Call tools via `tools.<name>(**args)` (e.g., `tools.grep(pattern="TODO", path="vibe")`,
  `tools.read_file(path="README.md", offset=0, limit=50)`).
- Tool results support both dictionary (`res["content"]`) and attribute
  (`res.content`) access. For `grep` and `find`, `res.matches` is a string that
  also iterates line-by-line where each item provides `m["path"]`, `m["line"]`,
  and `m["text"]`.
- Use `print(...)` to emit the final summary into `output`.
- Preloaded modules: `json`, `re`, `math`, `statistics`.
- `import`, `open`, `eval`, `exec`, `compile`, and network/filesystem access are
  disabled; all external access must go through `tools.*`.

## Example

```python
paths = [m["path"] for m in tools.grep(pattern="TODO", path="vibe").matches]
todo = sorted({p for p in paths if p.endswith(".py")})
print(f"{len(todo)} Python files with TODO")
for p in todo[:5]:
    print(p)
```

## Resource limits

- `timeout` (default 30s), `max_calls` (default 20), `max_output_bytes`
  (default 16384), and virtual memory (`memory_mb`, default 256MB) are enforced
  automatically.
