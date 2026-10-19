from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vibe.core.autocompletion.fuzzy import fuzzy_match
from vibe.core.config import ModelConfig

DIALOG_CLASS = "model-dialog"
TITLE_CLASS = "model-title"
HINT_CLASS = "model-hint"
SEARCH_CLASS = "model-search"
LIST_CLASS = "model-list"
OPTION_CLASS = "model-option"
SELECTED_CLASS = "model-option-selected"
CURRENT_CLASS = "model-option-current"
EMPTY_CLASS = "model-empty"
HELP_CLASS = "model-help"


class ModelSelectorScreen(ModalScreen[str | None]):
    """Modal model picker for choosing the active model alias.

    It offers the models the user can actually run: models that are in scope
    (``enabled_models``) *and* belong to a provider with usable credentials.
    The list is fuzzy searchable and keeps the highlighted row scrolled into
    view while navigating.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self, models: list[ModelConfig], active_alias: str, *, hint: str | None = None
    ) -> None:
        super().__init__()
        self.models = models
        self.active_alias = active_alias
        self.hint = hint
        self.filtered = list(models)
        self.selected_index = self._initial_index()
        self.rows: list[tuple[ModelConfig, Static]] = []

    def _initial_index(self) -> int:
        return next(
            (
                index
                for index, model in enumerate(self.models)
                if model.alias == self.active_alias
            ),
            0,
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes=DIALOG_CLASS):
            yield Static("Select model", classes=TITLE_CLASS)
            if self.hint:
                yield Static(self.hint, classes=HINT_CLASS)
            yield Input(placeholder="Search models…", classes=SEARCH_CLASS)
            with VerticalScroll(classes=LIST_CLASS):
                for model in self.models:
                    widget = Static("", classes=OPTION_CLASS)
                    self.rows.append((model, widget))
                    yield widget
                yield Static("  No models available.", classes=EMPTY_CLASS)
            yield Static("↑↓ navigate  Enter select  Esc cancel", classes=HELP_CLASS)

    def on_mount(self) -> None:
        self._update_display()
        self.query_one(f".{SEARCH_CLASS}", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_select()

    def _apply_filter(self, pattern: str) -> None:
        needle = pattern.strip()
        scored = [
            (match.score, index, model)
            for index, model in enumerate(self.models)
            if (match := fuzzy_match(needle, self._searchable_text(model))).matched
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        self.filtered = [model for _, _, model in scored]
        self.selected_index = 0
        self._update_display()

    @staticmethod
    def _searchable_text(model: ModelConfig) -> str:
        return f"{model.alias} {model.provider}/{model.name} {model.name}"

    def _update_display(self) -> None:
        visible = {id(model) for model in self.filtered}
        selected = self.filtered[self.selected_index] if self.filtered else None
        for model, widget in self.rows:
            is_selected = model is selected
            widget.display = id(model) in visible
            widget.update(self._format_model(model, selected=is_selected))
            widget.set_class(is_selected, SELECTED_CLASS)
            widget.set_class(model.alias == self.active_alias, CURRENT_CLASS)
        self.query_one(f".{EMPTY_CLASS}", Static).display = bool(self.filtered)
        self._scroll_selected_into_view()

    def _format_model(self, model: ModelConfig, *, selected: bool) -> str:
        cursor = "›" if selected else " "
        current = "  [dim]• current[/dim]" if model.alias == self.active_alias else ""
        detail = f"  [dim]{model.provider} · {model.context_window // 1000}K[/dim]"
        return f"{cursor} {model.alias}{current}{detail}"

    def _scroll_selected_into_view(self) -> None:
        if not self.filtered:
            return
        selected = self.filtered[self.selected_index]
        for model, widget in self.rows:
            if model is selected:
                widget.scroll_visible(animate=False)
                return

    def action_move_up(self) -> None:
        if not self.filtered:
            return
        self.selected_index = (self.selected_index - 1) % len(self.filtered)
        self._update_display()

    def action_move_down(self) -> None:
        if not self.filtered:
            return
        self.selected_index = (self.selected_index + 1) % len(self.filtered)
        self._update_display()

    def action_select(self) -> None:
        if not self.filtered:
            return
        self.dismiss(self.filtered[self.selected_index].alias)

    def action_cancel(self) -> None:
        self.dismiss(None)
