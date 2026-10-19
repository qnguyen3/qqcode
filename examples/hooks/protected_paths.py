"""Deny edits/writes to protected paths such as ``.env`` and ``secrets/**``.

Copy (or symlink) this file into ``.qqcode/hooks/`` or ``~/.qqcode/hooks/`` to
enable it. Edit ``PROTECTED_PATTERNS`` and ``PATH_ARGUMENTS`` to taste.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from vibe.core.hooks.api import deny, on_tool_call
from vibe.core.hooks.events import HookDecision, ToolCallEventHook

PROTECTED_PATTERNS = (
    ".env",
    ".env.*",
    "secrets/**",
    "**/secrets/**",
    "*.pem",
    "*.key",
)

PATH_ARGUMENTS = {
    "write_file": "path",
    "edit": "file_path",
}


def _is_protected(raw_path: str) -> bool:
    path = Path(raw_path)
    return any(
        fnmatch(path.name, pattern) or fnmatch(str(path), pattern)
        for pattern in PROTECTED_PATTERNS
    )


@on_tool_call()
def protect_paths(event: ToolCallEventHook) -> HookDecision | None:
    argument = PATH_ARGUMENTS.get(event.tool_name)
    if argument is None:
        return None

    raw_path = str(event.args.get(argument, "")).strip()
    if not raw_path or not _is_protected(raw_path):
        return None

    return deny(f"Path '{raw_path}' is protected by the protected-paths hook")
