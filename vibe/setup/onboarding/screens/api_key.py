from __future__ import annotations

import os
from typing import ClassVar

from dotenv import set_key
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Horizontal, Vertical
from textual.events import MouseUp
from textual.validation import Length
from textual.widgets import Input, Link, Static

from vibe.cli.clipboard import copy_selection_to_clipboard
from vibe.core.config import GLOBAL_ENV_FILE, ProviderConfig, VibeConfig
from vibe.setup.onboarding.base import OnboardingScreen

PROVIDER_HELP = {
    "mistral": ("https://console.mistral.ai/codestral/vibe", "Mistral AI Console"),
    "openrouter": ("https://openrouter.ai/keys", "OpenRouter Dashboard"),
}

PROVIDER_DISPLAY_NAMES = {
    "mistral": "Mistral",
    "openrouter": "OpenRouter",
    "llamacpp": "Local (llama.cpp)",
}

DEFAULT_MODELS_BY_PROVIDER = {
    "mistral": "devstral-2",
    "openrouter": "openrouter/gpt-5.2",
    "llamacpp": "local",
}

CONFIG_DOCS_URL = (
    "https://github.com/mistralai/mistral-vibe?tab=readme-ov-file#configuration"
)

VISIBLE_NEIGHBORS = 1
FADE_CLASSES = ["fade-1"]


def _save_api_key_to_env_file(env_key: str, api_key: str) -> None:
    GLOBAL_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    set_key(GLOBAL_ENV_FILE, env_key, api_key)


class ApiKeyScreen(OnboardingScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("up", "prev_provider", "Previous", show=False),
        Binding("down", "next_provider", "Next", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    NEXT_SCREEN = None

    def __init__(self) -> None:
        super().__init__()
        self.config = VibeConfig.model_construct()
        self.providers = list(self.config.providers)
        self._provider_index = 0
        self._provider_widgets: list[Static] = []

        # Set initial selection based on current active model
        active_model = self.config.get_active_model()
        active_provider = self.config.get_provider_for_model(active_model)
        for i, p in enumerate(self.providers):
            if p.name == active_provider.name:
                self._provider_index = i
                break

    @property
    def provider(self) -> ProviderConfig:
        return self.providers[self._provider_index]

    @property
    def needs_api_key(self) -> bool:
        return bool((self.provider.api_key_env_var or "").strip())

    def _get_display_name(self, provider: ProviderConfig) -> str:
        return PROVIDER_DISPLAY_NAMES.get(provider.name, provider.name.capitalize())

    def _compose_provider_selector(self) -> ComposeResult:
        for _ in range(VISIBLE_NEIGHBORS * 2 + 1):
            widget = Static("", classes="provider-item")
            self._provider_widgets.append(widget)
            yield widget

    def _compose_provider_link(self) -> ComposeResult:
        # This will be dynamically updated
        yield Static("", id="provider-link-text")
        yield Center(
            Horizontal(
                Static("→ ", classes="link-chevron"),
                Link("", url="", id="provider-link"),
                classes="link-row",
            ),
            id="provider-link-row",
        )

    def _compose_config_docs(self) -> ComposeResult:
        yield Static("[dim]Learn more about Vibe configuration:[/]")
        yield Horizontal(
            Static("→ ", classes="link-chevron"),
            Link(CONFIG_DOCS_URL, url=CONFIG_DOCS_URL),
            classes="link-row",
        )

    def compose(self) -> ComposeResult:
        self.input_widget = Input(
            password=True,
            id="key",
            placeholder="Paste your API key here",
            validators=[Length(minimum=1, failure_description="No API key provided.")],
        )

        with Vertical(id="api-key-outer"):
            yield Static("", classes="spacer")
            yield Center(Static("Select your provider", id="api-key-title"))
            with Center():
                with Vertical(id="api-key-content"):
                    # Provider selector section
                    yield Center(
                        Horizontal(
                            Static("Navigate ↑ ↓", id="nav-hint"),
                            Vertical(
                                *self._compose_provider_selector(), id="provider-list"
                            ),
                            Static("", id="enter-hint"),
                            id="provider-row",
                        )
                    )

                    # API key section (hidden for local provider)
                    with Vertical(id="api-key-section"):
                        yield from self._compose_provider_link()
                        yield Static(
                            "...and paste your API key below:", id="paste-hint"
                        )
                        yield Center(Horizontal(self.input_widget, id="input-box"))
                        yield Static("", id="feedback")

                    # Local provider confirmation section
                    with Vertical(id="local-section", classes="hidden"):
                        yield Static(
                            "No API key needed for local inference.", id="local-info"
                        )
                        yield Static(
                            "[dim]Make sure llama.cpp server is running on port 8080[/]",
                            id="local-hint",
                        )

            yield Static("", classes="spacer")
            yield Vertical(
                Vertical(*self._compose_config_docs(), id="config-docs-group"),
                id="config-docs-section",
            )

    def on_mount(self) -> None:
        self._update_provider_display()
        self._update_focus()

    def _get_provider_at_offset(self, offset: int) -> ProviderConfig:
        index = (self._provider_index + offset) % len(self.providers)
        return self.providers[index]

    def _update_provider_display(self) -> None:
        """Update carousel display and toggle sections based on selected provider."""
        # Update carousel items
        for i, widget in enumerate(self._provider_widgets):
            offset = i - VISIBLE_NEIGHBORS
            provider = self._get_provider_at_offset(offset)
            display_name = self._get_display_name(provider)

            widget.remove_class("selected", *FADE_CLASSES)

            if offset == 0:
                widget.update(f" {display_name} ")
                widget.add_class("selected")
            else:
                distance = min(abs(offset) - 1, len(FADE_CLASSES) - 1)
                widget.update(display_name)
                if FADE_CLASSES:
                    widget.add_class(FADE_CLASSES[distance])

        # Toggle sections based on whether provider needs API key
        api_key_section = self.query_one("#api-key-section", Vertical)
        local_section = self.query_one("#local-section", Vertical)
        enter_hint = self.query_one("#enter-hint", Static)

        if self.needs_api_key:
            api_key_section.remove_class("hidden")
            local_section.add_class("hidden")
            enter_hint.update("")
            self._update_provider_link()
        else:
            api_key_section.add_class("hidden")
            local_section.remove_class("hidden")
            enter_hint.update("Press Enter ↵")

    def _update_provider_link(self) -> None:
        """Update the provider help link based on selected provider."""
        link_text = self.query_one("#provider-link-text", Static)
        link = self.query_one("#provider-link", Link)
        link_row = self.query_one("#provider-link-row", Center)

        provider_name = self._get_display_name(self.provider)

        if self.provider.name in PROVIDER_HELP:
            help_url, help_name = PROVIDER_HELP[self.provider.name]
            link_text.update(f"Grab your {provider_name} API key from the {help_name}:")
            link.update(help_url)
            link.url = help_url
            link_row.remove_class("hidden")
        else:
            link_text.update(f"Enter your {provider_name} API key:")
            link_row.add_class("hidden")

    def _update_focus(self) -> None:
        """Set focus appropriately based on selected provider."""
        if self.needs_api_key:
            self.input_widget.focus()
        else:
            self.focus()

    def action_next_provider(self) -> None:
        self._provider_index = (self._provider_index + 1) % len(self.providers)
        self._update_provider_display()
        self._update_focus()

    def action_prev_provider(self) -> None:
        self._provider_index = (self._provider_index - 1) % len(self.providers)
        self._update_provider_display()
        self._update_focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        feedback = self.query_one("#feedback", Static)
        input_box = self.query_one("#input-box")

        if event.validation_result is None:
            return

        input_box.remove_class("valid", "invalid")
        feedback.remove_class("error", "success")

        if event.validation_result.is_valid:
            feedback.update("Press Enter to submit ↵")
            feedback.add_class("success")
            input_box.add_class("valid")
            return

        descriptions = event.validation_result.failure_descriptions
        feedback.update(descriptions[0])
        feedback.add_class("error")
        input_box.add_class("invalid")

    def action_submit(self) -> None:
        """Handle Enter key - either submit API key or confirm local selection."""
        if not self.needs_api_key:
            # Local provider - no API key needed, just save selection
            self._save_provider_selection()
            self.app.exit("completed")
        else:
            # For API key providers, validate and submit if valid
            validation = self.input_widget.validate(self.input_widget.value)
            if validation and validation.is_valid:
                self._save_and_finish(self.input_widget.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.validation_result and event.validation_result.is_valid:
            self._save_and_finish(event.value)

    def _save_provider_selection(self) -> None:
        """Save the active_model based on selected provider."""
        provider_name = self.provider.name
        default_model = DEFAULT_MODELS_BY_PROVIDER.get(provider_name)

        if default_model:
            try:
                VibeConfig.save_updates({"active_model": default_model})
            except OSError:
                pass  # Non-fatal - config will use defaults

    def _save_and_finish(self, api_key: str) -> None:
        env_key = (self.provider.api_key_env_var or "").strip()
        if not env_key:
            self._save_provider_selection()
            self.app.exit("completed")
            return

        # Save API key to environment
        os.environ[env_key] = api_key
        try:
            _save_api_key_to_env_file(env_key, api_key)
        except OSError as err:
            self.app.exit(f"save_error:{err}")
            return

        # Save active model selection
        self._save_provider_selection()
        self.app.exit("completed")

    def on_mouse_up(self, event: MouseUp) -> None:
        copy_selection_to_clipboard(self.app)
