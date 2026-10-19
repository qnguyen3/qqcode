# Prompt Templates

Prompt templates are markdown files that become slash commands. They expand
parameter placeholders and are sent to the agent as a regular user message,
which makes them a lightweight way to package repeated prompts such as
"review this diff" or "open a PR".

## Locations

Templates are loaded from, in priority order (first match wins):

1. Directories passed programmatically (used by `--prompt-template DIR` in
   plan 07).
2. `.qqcode/prompts/commands/` in the current project (searching from the
   working directory up to the filesystem root, first directory found).
3. `~/.qqcode/prompts/commands/`.

Each `*.md` file becomes `/<filename-without-extension>`. For example
`.qqcode/prompts/commands/pr.md` registers `/pr`. Template file names must
match `^[a-z][a-z0-9_-]*$`; other files are ignored.

If a template name collides with a built-in command (for example `help.md`),
the built-in command wins and a warning is logged.

## Frontmatter

Templates support a minimal frontmatter block (parsed with a regex, no YAML
dependency):

```markdown
---
description: Create a pull request
argument-hint: <branch> [base]
---

Open a pull request for `$1` targeting `${2:-main}`.
```

- `description`: shown in `/help` and autocompletion.
- `argument-hint`: documentation only; it is not displayed by the UI yet.

## Placeholders

Arguments are tokenized shell-style, so single and double quotes group words:

| Placeholder | Expands to |
|---|---|
| `$1` … `$9` | Positional argument (empty when missing) |
| `$@`, `$ARGUMENTS` | All arguments joined by spaces |
| `${N:-default}` | Argument `N`, or `default` when missing/empty |
| `${@:-default}` | All arguments, or `default` when there are none |
| `${@:N}` | Arguments from position `N` to the end |
| `${@:N:L}` | `L` arguments starting at position `N` |

Example:

```markdown
---
description: Review files against a base branch
---

Review $@ against ${1:-main}. Focus on $2.
```

```text
> /review "src/app.py" correctness
```

expands to:

```text
Review src/app.py against main. Focus on correctness.
```

## Notes

- Template content is sent as a user message, exactly like typed input.
- Context files (`AGENTS.md`, `CLAUDE.md`, …) and skills are independent
  features; see the README for details.
