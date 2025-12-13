from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, ScrollableContainer, Vertical
from textual.message import Message
from textual.widgets import Input, Markdown, Static

from vibe.core.types import AgentMode

_REVISE_OPTION_INDEX = 2  # Index of the "Revise Plan" option


class PlanApprovalWidget(Container):
    """Widget for approving or revising a plan after Plan Mode."""

    can_focus = True
    can_focus_children = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("1", "select_1", "Execute (Auto)", show=False),
        Binding("2", "select_2", "Execute (Manual)", show=False),
        Binding("3", "select_3", "Revise Plan", show=False),
        Binding("r", "select_3", "Revise Plan", show=False),
        Binding("escape", "exit_revision", "Exit revision mode", show=False),
    ]

    class PlanApproved(Message):
        """Message sent when user approves the plan."""

        def __init__(self, mode: AgentMode) -> None:
            super().__init__()
            self.mode = mode

    class RevisionRequested(Message):
        """Message sent when user wants to revise the plan."""

        def __init__(self, feedback: str) -> None:
            super().__init__()
            self.feedback = feedback

    def __init__(self, plan: str | None = None) -> None:
        super().__init__(id="plan-approval-app")
        self._plan = plan
        self.selected_option = 0
        self.option_widgets: list[Static] = []
        self.help_widget: Static | None = None
        self._revision_input: Input | None = None
        self._in_revision_mode = False

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-approval-content"):
            yield Static("Plan Ready", classes="plan-approval-title")

            # Show the plan content if available
            if self._plan:
                yield Static(
                    "Review the plan below:",
                    classes="plan-approval-subtitle",
                )
                with ScrollableContainer(id="plan-content-container"):
                    yield Markdown(self._plan, id="plan-content")
            else:
                yield Static(
                    "The plan is ready. How would you like to proceed?",
                    classes="plan-approval-subtitle",
                )

            yield Static("")

            for _ in range(3):
                widget = Static("", classes="plan-approval-option")
                self.option_widgets.append(widget)
                yield widget

            # Revision input (hidden by default)
            self._revision_input = Input(
                placeholder="Type your revision feedback and press Enter...",
                id="revision-input",
            )
            self._revision_input.display = False
            yield self._revision_input

            yield Static("")

            self.help_widget = Static(
                "Use arrow keys to navigate, Enter to select",
                classes="plan-approval-help",
            )
            yield self.help_widget

    def on_mount(self) -> None:
        self._update_options()
        self.focus()

    def _update_options(self) -> None:
        options = [
            ("Execute with Auto-Approve", "auto", "Run the plan automatically"),
            ("Execute with Manual Approval", "manual", "Review each step"),
            ("Revise Plan", "revise", "Give feedback to improve the plan"),
        ]

        for idx, ((text, option_type, _desc), widget) in enumerate(
            zip(options, self.option_widgets, strict=True)
        ):
            is_selected = idx == self.selected_option

            cursor = "› " if is_selected else "  "

            # Show revision hint when in revision mode and on revise option
            if self._in_revision_mode and idx == _REVISE_OPTION_INDEX:
                option_text = f"{cursor}{idx + 1}. {text}: (type below)"
            else:
                option_text = f"{cursor}{idx + 1}. {text}"

            widget.update(option_text)

            # Remove all classes first
            widget.remove_class(
                "plan-option-selected",
                "plan-option-auto",
                "plan-option-manual",
                "plan-option-revise",
            )

            if is_selected:
                widget.add_class("plan-option-selected")

            match option_type:
                case "auto":
                    widget.add_class("plan-option-auto")
                case "manual":
                    widget.add_class("plan-option-manual")
                case "revise":
                    widget.add_class("plan-option-revise")

    def action_move_up(self) -> None:
        if self._in_revision_mode:
            return  # Don't navigate when typing
        self.selected_option = (self.selected_option - 1) % 3
        self._update_options()

    def action_move_down(self) -> None:
        if self._in_revision_mode:
            return  # Don't navigate when typing
        self.selected_option = (self.selected_option + 1) % 3
        self._update_options()

    def action_select(self) -> None:
        if self._in_revision_mode:
            return  # Let input handle enter
        self._handle_selection(self.selected_option)

    def action_select_1(self) -> None:
        if self._in_revision_mode:
            return
        self.selected_option = 0
        self._handle_selection(0)

    def action_select_2(self) -> None:
        if self._in_revision_mode:
            return
        self.selected_option = 1
        self._handle_selection(1)

    def action_select_3(self) -> None:
        self.selected_option = 2
        self._update_options()
        self._enter_revision_mode()

    def action_exit_revision(self) -> None:
        """Exit revision mode without submitting."""
        if self._in_revision_mode:
            self._exit_revision_mode()

    def _enter_revision_mode(self) -> None:
        """Show input field for revision feedback."""
        self._in_revision_mode = True
        if self._revision_input:
            self._revision_input.display = True
            self._revision_input.focus()
        if self.help_widget:
            self.help_widget.update("Type your feedback and press Enter (Esc to cancel)")
        self._update_options()

    def _exit_revision_mode(self) -> None:
        """Hide input field and return to option selection."""
        self._in_revision_mode = False
        if self._revision_input:
            self._revision_input.display = False
            self._revision_input.value = ""
        if self.help_widget:
            self.help_widget.update("Use arrow keys to navigate, Enter to select")
        self._update_options()
        self.focus()

    def _handle_selection(self, option: int) -> None:
        match option:
            case 0:
                # Execute with auto-approve
                self.post_message(self.PlanApproved(mode=AgentMode.AUTO_APPROVE))
            case 1:
                # Execute with manual approval
                self.post_message(self.PlanApproved(mode=AgentMode.INTERACTIVE))
            case 2:
                # Enter revision mode instead of immediately posting
                self._enter_revision_mode()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter press in revision input."""
        if self._in_revision_mode and event.value.strip():
            self.post_message(self.RevisionRequested(feedback=event.value.strip()))
            self._exit_revision_mode()

    def on_blur(self, event: events.Blur) -> None:
        # Don't refocus if we're in revision mode (input has focus)
        if not self._in_revision_mode:
            self.call_after_refresh(self.focus)
