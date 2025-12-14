Use `edit` to make targeted changes to files by replacing exact text matches.

Arguments:
- `file_path`: The path to the file to modify
- `old_string`: The exact text to find in the file
- `new_string`: The text to replace it with
- `replace_all`: (optional) Set to true to replace all occurrences. Default is false.

Example usage:

```
file_path: "src/utils.py"
old_string: "def old_function():"
new_string: "def new_function():"
```

To replace all occurrences:

```
file_path: "src/config.py"
old_string: "DEBUG = True"
new_string: "DEBUG = False"
replace_all: true
```

IMPORTANT:

- The `old_string` must match EXACTLY (including whitespace and indentation)
- By default, `old_string` must appear exactly once in the file. If it appears multiple times, the tool will error unless you set `replace_all: true`
- If `old_string` is not found, the tool will error
- Use `read_file` first to see the exact content before editing
- For creating new files, use `write_file` instead
