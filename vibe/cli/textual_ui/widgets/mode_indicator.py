from __future__ import annotations

from textual.widgets import Static

from vibe.core.types import AgentMode


class ModeIndicator(Static):
    def __init__(self, mode: AgentMode = AgentMode.INTERACTIVE) -> None:
        super().__init__()
        self.can_focus = False
        self._mode = mode
        self._update_display()

    def _update_display(self) -> None:
        # Remove all mode classes first
        self.remove_class("mode-off", "mode-on", "mode-plan")

        match self._mode:
            case AgentMode.INTERACTIVE:
                self.update("⏵ interactive (shift+tab to toggle)")
                self.add_class("mode-off")
            case AgentMode.AUTO_APPROVE:
                self.update("⏵⏵ auto-approve (shift+tab to toggle)")
                self.add_class("mode-on")
            case AgentMode.PLAN:
                self.update("⌥ plan mode (shift+tab to toggle)")
                self.add_class("mode-plan")

    def set_mode(self, mode: AgentMode) -> None:
        """Set the current mode and update display."""
        self._mode = mode
        self._update_display()

    def set_auto_approve(self, enabled: bool) -> None:
        """Legacy method for backward compatibility."""
        if enabled:
            self.set_mode(AgentMode.AUTO_APPROVE)
        elif self._mode == AgentMode.AUTO_APPROVE:
            self.set_mode(AgentMode.INTERACTIVE)

    @property
    def mode(self) -> AgentMode:
        """Get the current mode."""
        return self._mode
