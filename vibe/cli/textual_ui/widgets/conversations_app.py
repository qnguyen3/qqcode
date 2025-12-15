from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.message import Message
from textual.widgets import Static

# Maximum characters for message preview
_MAX_MESSAGE_LENGTH = 50


def _format_datetime(iso_string: str) -> str:
    """Format ISO datetime string to readable format like 'Dec 15, 09:03'."""
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return "Unknown"


def _truncate_message(message: str, max_length: int = _MAX_MESSAGE_LENGTH) -> str:
    """Truncate message to max_length with ellipsis."""
    # Remove newlines and extra whitespace
    message = " ".join(message.split())
    if len(message) <= max_length:
        return message
    return message[: max_length - 3] + "..."


class ConversationsApp(Container):
    can_focus = True
    can_focus_children = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select_session", "Select", show=False),
    ]

    class ConversationSelected(Message):
        """Message emitted when a conversation is selected."""

        def __init__(self, filepath: Path, session_id: str) -> None:
            super().__init__()
            self.filepath = filepath
            self.session_id = session_id

    class ConversationsClosed(Message):
        """Message emitted when the conversations dialog is closed."""

    def __init__(
        self, sessions: list[tuple[Path, dict[str, Any]]], **kwargs: Any
    ) -> None:
        super().__init__(id="conversations-app", **kwargs)
        self.sessions = sessions
        self.selected_index = 0
        self.title_widget: Static | None = None
        self.session_widgets: list[Static] = []
        self.help_widget: Static | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="conversations-content"):
            self.title_widget = Static(
                "Past Conversations", classes="conversations-title"
            )
            yield self.title_widget

            yield Static("")

            if not self.sessions:
                yield Static(
                    "  No past sessions found.", classes="conversations-empty"
                )
            else:
                for _ in self.sessions:
                    widget = Static("", classes="conversations-option")
                    self.session_widgets.append(widget)
                    yield widget

            yield Static("")

            self.help_widget = Static(
                "↑↓ navigate  Enter select  ESC cancel", classes="conversations-help"
            )
            yield self.help_widget

    def on_mount(self) -> None:
        self._update_display()
        self.focus()

    def _update_display(self) -> None:
        """Update the display of all session entries."""
        for i, (widget, (_filepath, summary)) in enumerate(
            zip(self.session_widgets, self.sessions, strict=True)
        ):
            is_selected = i == self.selected_index
            cursor = "> " if is_selected else "  "

            message = _truncate_message(summary.get("last_user_message", ""))
            end_time = _format_datetime(summary.get("end_time", ""))
            session_id = summary.get("session_id", "unknown")

            # Format: "> message...     Dec 15, 09:03   f23faea4"
            # Pad message to consistent width for alignment
            padded_message = message.ljust(_MAX_MESSAGE_LENGTH)
            text = f"{cursor}{padded_message}  {end_time}  {session_id}"

            widget.update(text)

            widget.remove_class("conversations-selected")
            widget.remove_class("conversations-unselected")

            if is_selected:
                widget.add_class("conversations-selected")
            else:
                widget.add_class("conversations-unselected")

    def action_move_up(self) -> None:
        if not self.sessions:
            return
        self.selected_index = (self.selected_index - 1) % len(self.sessions)
        self._update_display()

    def action_move_down(self) -> None:
        if not self.sessions:
            return
        self.selected_index = (self.selected_index + 1) % len(self.sessions)
        self._update_display()

    def action_select_session(self) -> None:
        if not self.sessions:
            return
        filepath, summary = self.sessions[self.selected_index]
        session_id = summary.get("session_id", "unknown")
        self.post_message(self.ConversationSelected(filepath, session_id))

    def action_close(self) -> None:
        self.post_message(self.ConversationsClosed())

    def on_blur(self, event: events.Blur) -> None:
        self.call_after_refresh(self.focus)
