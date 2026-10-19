Use the `find` tool to locate files and directories by glob pattern.

Arguments:
- `pattern`: Glob pattern matched against each full path (e.g. `*.py`, `**/test_*.py`).
- `path`: (optional) Directory to search in. Defaults to `.`.
- `limit`: (optional) Maximum number of matches to return. Defaults to 1000.

**Key characteristics:**
- `.gitignore` and common ignore directories (`.git`, `node_modules`, `__pycache__`, ...) are respected.
- Results are paths relative to `path`, sorted alphabetically; directories end with `/`.
- Uses `fd` when installed, otherwise a built-in walk.

**Example usage:**
```
find(pattern="*.py", path="src")
find(pattern="**/test_*.py", limit=50)
```

**Important:** Do not shell out to `find`, `fd`, `ls`, or `rg` through the `bash` tool for file discovery; use `find`, `ls`, and `grep` instead.
