from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.widgets import Static

# Max characters for tool call/result display before truncation
_MAX_DISPLAY_LENGTH = 200
_MAX_ARG_VALUE_LENGTH = 30
_TRUNCATION_ELLIPSIS = "..."


def _truncate_text(text: str, max_length: int = _MAX_DISPLAY_LENGTH) -> str:
    """Truncate text to max_length with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(_TRUNCATION_ELLIPSIS)] + _TRUNCATION_ELLIPSIS


def _format_args(args_str: str) -> str:
    """Format arguments string for display."""
    try:
        args = json.loads(args_str)
        if isinstance(args, dict):
            parts = []
            for k, v in list(args.items())[:3]:
                v_str = str(v)
                if len(v_str) > _MAX_ARG_VALUE_LENGTH:
                    v_str = v_str[: _MAX_ARG_VALUE_LENGTH - 3] + "..."
                parts.append(f"{k}={v_str!r}")
            return ", ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    return _truncate_text(args_str, 100)


class HistoricalToolCall(Static):
    """Display a tool call from history (static, no blinking)."""

    def __init__(self, tool_name: str, args_str: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args_str = args_str
        self.add_class("historical-tool-call")

    def compose(self) -> ComposeResult:
        formatted_args = _format_args(self.args_str)
        display_text = f"↳ {self.tool_name}({formatted_args})"
        yield Static(_truncate_text(display_text), markup=False)


class HistoricalToolResult(Static):
    """Display a tool result from history."""

    def __init__(
        self, tool_name: str, content: str, is_error: bool = False
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.content = content
        self.is_error = is_error
        self.add_class("historical-tool-result")
        if is_error:
            self.add_class("historical-tool-result-error")

    def compose(self) -> ComposeResult:
        if self.is_error:
            display_text = "  ✗ Error"
        else:
            # Just show a success indicator for historical results
            # Full content would be too verbose
            display_text = "  ✓ Done"
        yield Static(display_text, markup=False)
