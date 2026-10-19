Use `read_file` to read the content of a file. It's designed to handle large files safely.

- By default, it reads from the beginning of the file.
- Use `offset` (line number) and `limit` (number of lines) to read specific parts or chunks of a file. This is efficient for exploring large files.
- When output is truncated, the result contains a notice with the shown line range and the next `offset` to continue from.

**Strategy for large files:**

1. Call `read_file` with a `limit` (e.g., 1000 lines) to get the start of the file.
2. If the result is truncated, the notice tells you the next `offset` to use.
3. To read the next chunk, call `read_file` again with that `offset` and a `limit`. For example, `offset=1000, limit=1000`.

This is more efficient than using `bash` with `cat` or `wc`.
