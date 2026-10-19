from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from vibe.core.trust import ProjectResources, TrustChoice


class TrustScreen(ModalScreen[TrustChoice]):
    """Modal asking whether project ``.qqcode`` resources may load."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer_yes", "Yes", show=True),
        Binding("n", "answer_no", "No", show=True),
        Binding("a", "answer_always", "Always", show=True),
        Binding("escape", "answer_no", "No", show=False),
    ]

    def __init__(self, project: ProjectResources) -> None:
        super().__init__(id="trust-screen")
        self.project = project

    def compose(self) -> ComposeResult:
        labels = ", ".join(self.project.resource_labels) or "none"
        with Vertical(id="trust-dialog"):
            yield Static("Trust project?", id="trust-title")
            yield Static(
                f"Project: {self.project.project_dir}\n"
                f"Resources: {labels}\n\n"
                "Project resources can execute code (hooks, custom tools, MCP "
                "servers). Context files (AGENTS.md) always load.",
                id="trust-body",
            )
            with Horizontal(id="trust-buttons"):
                yield Button("Yes", id="trust-yes", variant="success")
                yield Button("No", id="trust-no", variant="error")
                yield Button("Always", id="trust-always", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "trust-yes":
                self.dismiss(TrustChoice.YES)
            case "trust-always":
                self.dismiss(TrustChoice.ALWAYS)
            case _:
                self.dismiss(TrustChoice.NO)

    def action_answer_yes(self) -> None:
        self.dismiss(TrustChoice.YES)

    def action_answer_no(self) -> None:
        self.dismiss(TrustChoice.NO)

    def action_answer_always(self) -> None:
        self.dismiss(TrustChoice.ALWAYS)
