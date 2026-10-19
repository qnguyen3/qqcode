The messages above are NEW conversation messages. Update the existing summary provided in <previous_summary> tags.

Rules:
- Preserve all existing information from the previous summary.
- Add new progress, decisions, and context from the new messages.
- Update the Progress section: move items from "In Progress" to "Done" when completed.
- Update "Next Steps" based on what was accomplished.
- Preserve exact file paths, function names, commands, and error messages.
- Do not invent information; if something is not present in the conversation, write "(none)".
- Respond with ONLY the updated summary text in the format below. Do not continue the conversation.

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing constraints and preferences, add new ones discovered]

## Progress
### Done
- [x] [Previously done items AND newly completed items]

### In Progress
- [ ] [Current work]

### Blocked
- [Current blockers, removed if resolved. "(none)" if none]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Updated ordered list of what should happen next]

## Critical Context
- [Preserve important context, add new details if needed. "(none)" if not applicable]
