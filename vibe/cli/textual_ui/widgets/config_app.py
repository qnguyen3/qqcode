from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, TypedDict

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.message import Message
from textual.theme import BUILTIN_THEMES
from textual.widgets import Static

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

THEMES = sorted(k for k in BUILTIN_THEMES if k != "textual-ansi")


class SettingDefinition(TypedDict):
    key: str
    label: str
    type: str
    options: list[str]
    value: str


class ConfigApp(Container):
    can_focus = True
    can_focus_children = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("space", "toggle_setting", "Toggle", show=False),
        Binding("enter", "cycle", "Next", show=False),
    ]

    class SettingChanged(Message):
        def __init__(self, key: str, value: str) -> None:
            super().__init__()
            self.key = key
            self.value = value

    class ConfigClosed(Message):
        def __init__(self, changes: dict[str, str]) -> None:
            super().__init__()
            self.changes = changes

    def __init__(self, config: VibeConfig) -> None:
        super().__init__(id="config-app")
        self.config = config
        self.selected_index = 0
        self.changes: dict[str, str] = {}

        provider_options = self._provider_options()
        inferred_provider = self._infer_active_provider()
        if inferred_provider not in provider_options and provider_options:
            inferred_provider = provider_options[0]

        model_options = self._model_options_for_provider(inferred_provider)

        # If the configured active model doesn't match the selected provider,
        # default to the first available model for that provider.
        initial_model = self.config.active_model
        if model_options and initial_model not in model_options:
            initial_model = model_options[0]
            self.changes["active_model"] = initial_model

        self.settings: list[SettingDefinition] = [
            {
                "key": "active_provider",
                "label": "Provider",
                "type": "cycle",
                "options": provider_options,
                "value": inferred_provider,
            },
            {
                "key": "active_model",
                "label": "Model",
                "type": "cycle",
                "options": model_options,
                "value": initial_model,
            },
            {
                "key": "textual_theme",
                "label": "Theme",
                "type": "cycle",
                "options": THEMES,
                "value": self.config.textual_theme,
            },
        ]

        self.title_widget: Static | None = None
        self.setting_widgets: list[Static] = []
        self.help_widget: Static | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="config-content"):
            self.title_widget = Static("Settings", classes="settings-title")
            yield self.title_widget

            yield Static("")

            for _ in self.settings:
                widget = Static("", classes="settings-option")
                self.setting_widgets.append(widget)
                yield widget

            yield Static("")

            self.help_widget = Static(
                "↑↓ navigate  Space/Enter toggle  ESC exit", classes="settings-help"
            )
            yield self.help_widget

    def on_mount(self) -> None:
        self._sync_model_options_and_selection(post_messages=False)
        self._update_display()
        self.focus()

    def _get_setting(self, key: str) -> SettingDefinition:
        for setting in self.settings:
            if setting["key"] == key:
                return setting
        raise KeyError(f"Setting not found: {key}")

    def _get_value(self, key: str) -> str:
        setting = self._get_setting(key)
        return self.changes.get(key, setting["value"])

    def _provider_options(self) -> list[str]:
        providers_with_models = {m.provider for m in self.config.models}
        options = [
            p.name for p in self.config.providers if p.name in providers_with_models
        ]

        # If configuration is inconsistent (providers with 0 configured models),
        # fall back to listing all providers so users can still see them.
        if not options:
            options = [p.name for p in self.config.providers]

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for name in options:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    def _infer_active_provider(self) -> str:
        # Use saved active_provider if available
        if active_provider := getattr(self.config, "active_provider", None):
            return active_provider
        # Otherwise infer from active model
        try:
            return self.config.get_active_model().provider
        except Exception:
            options = self._provider_options()
            return options[0] if options else ""

    def _model_options_for_provider(self, provider: str) -> list[str]:
        return [m.alias for m in self.config.models if m.provider == provider]

    def _sync_model_options_and_selection(self, *, post_messages: bool) -> None:
        """Ensure Model options match selected Provider and selection is valid."""
        try:
            provider = self._get_value("active_provider")
            model_setting = self._get_setting("active_model")
        except KeyError:
            return

        options = self._model_options_for_provider(provider)
        model_setting["options"] = options

        if not options:
            return

        current_model = self._get_value("active_model")
        if current_model in options:
            return

        new_model = options[0]
        self.changes["active_model"] = new_model
        if post_messages:
            self.post_message(self.SettingChanged(key="active_model", value=new_model))

    def _update_display(self) -> None:
        for i, (setting, widget) in enumerate(
            zip(self.settings, self.setting_widgets, strict=True)
        ):
            is_selected = i == self.selected_index
            cursor = "› " if is_selected else "  "

            label: str = setting["label"]
            value: str = self.changes.get(setting["key"], setting["value"])

            text = f"{cursor}{label}: {value}"

            widget.update(text)

            widget.remove_class("settings-cursor-selected")
            widget.remove_class("settings-value-cycle-selected")
            widget.remove_class("settings-value-cycle-unselected")

            if is_selected:
                widget.add_class("settings-value-cycle-selected")
            else:
                widget.add_class("settings-value-cycle-unselected")

    def action_move_up(self) -> None:
        self.selected_index = (self.selected_index - 1) % len(self.settings)
        self._update_display()

    def action_move_down(self) -> None:
        self.selected_index = (self.selected_index + 1) % len(self.settings)
        self._update_display()

    def action_toggle_setting(self) -> None:
        setting = self.settings[self.selected_index]
        key: str = setting["key"]

        # Provider drives the available model list; ensure it's always synced before cycling.
        if key == "active_model":
            self._sync_model_options_and_selection(post_messages=False)

        current: str = self.changes.get(key, setting["value"])

        options: list[str] = setting["options"]
        try:
            current_idx = options.index(current)
            next_idx = (current_idx + 1) % len(options)
            new_value: str = options[next_idx]
        except (ValueError, IndexError):
            new_value: str = options[0] if options else current

        self.changes[key] = new_value
        self.post_message(self.SettingChanged(key=key, value=new_value))

        if key == "active_provider":
            # Update model options + ensure selected model is valid for provider.
            self._sync_model_options_and_selection(post_messages=True)

        self._update_display()

    def action_cycle(self) -> None:
        self.action_toggle_setting()

    def action_close(self) -> None:
        self.post_message(self.ConfigClosed(changes=self.changes.copy()))

    def on_blur(self, event: events.Blur) -> None:
        self.call_after_refresh(self.focus)
