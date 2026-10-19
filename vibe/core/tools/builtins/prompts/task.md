Use the `task` tool to delegate an independent, self-contained job to a subagent.

**When to use it:**
- The work is independent from the current conversation and can be explained in a
  single brief.
- The work needs many tool calls (exploration, searching, reading) that would
  otherwise fill your context with intermediate results.
- The work does not need user interaction or approval at any point.

**When NOT to use it:**
- The task depends on details only present in this conversation that you cannot
  summarize in the `prompt`.
- The task needs interactive approval, secrets, or destructive operations.
- A single quick tool call is enough — do it directly.

**Key rules:**
- The subagent does **not** see your history. Put everything it needs in
  `prompt`: goal, relevant file paths, constraints, and the expected output.
- The subagent runs with a restricted, read-only toolset by default, inside this
  project, and cannot delegate further.
- `description` must be 3-5 words; it is shown in the UI.
- `max_turns` caps the subagent's LLM turns (default 15).
- The subagent returns its final message text plus turn/token usage. Treat the
  returned text as a report, not as verified fact, and continue working from it.
