The messages above are a conversation to summarize. Create a structured context checkpoint summary that another model will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by the user]
- ["(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks or changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any. "(none)" if none]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Data, examples, or references needed to continue. "(none)" if not applicable]

Rules:
- Preserve exact file paths, function names, commands, and error messages.
- Do not invent information; if something is not present in the conversation, write "(none)".
- Respond with ONLY the summary text in the format above. Do not continue the conversation.
