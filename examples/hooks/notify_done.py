"""Send a desktop notification when an agent turn finishes.

Uses ``notify-send`` on Linux and ``osascript`` on macOS; silently does nothing
when neither tool is available.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from vibe.core.hooks.events import AgentEndEvent
from vibe.core.hooks.registry import HookRegistry

TITLE = "QQcode"


def _notify(title: str, message: str) -> None:
    if sys.platform == "darwin" and (osascript := shutil.which("osascript")):
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run([osascript, "-e", script], check=False, capture_output=True)
        return

    if notify_send := shutil.which("notify-send"):
        subprocess.run([notify_send, title, message], check=False, capture_output=True)


def on_agent_end(event: AgentEndEvent) -> None:
    session = event.session_id[:8]
    _notify(TITLE, f"Session {session} finished")


def register(registry: HookRegistry) -> None:
    registry.register("agent_end", on_agent_end)
