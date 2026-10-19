Use the `ls` tool to list the contents of a directory.

Arguments:
- `path`: (optional) Directory to list. Defaults to `.`.
- `limit`: (optional) Maximum number of entries to return. Defaults to 500.

**Key characteristics:**
- Entries are sorted alphabetically.
- Directories get a trailing `/`.
- Dotfiles are included.

**Example usage:**
```
ls(path="src")
ls(path=".", limit=100)
```

**Important:** Do not shell out to `ls` or `dir` through the `bash` tool; use the `ls` tool instead.
