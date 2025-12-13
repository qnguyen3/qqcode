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
from vibe.core.config import GLOBAL_ENV_FILE, VibeConfig
from vibe.setup.onboarding.base import OnboardingScreen

PROVIDER_HELP = {
    "mistral": ("https://console.mistral.ai/codestral/vibe", "Mistral AI Studio"),
    "openrouter": ("https://openrouter.ai/keys", "OpenRouter"),
}
CONFIG_DOCS_URL = (
    "https://github.com/mistralai/mistral-vibe?tab=readme-ov-file#configuration"
)


def _save_api_key_to_env_file(env_key: str, api_key: str) -> None:
    GLOBAL_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    set_key(GLOBAL_ENV_FILE, env_key, api_key)


class ApiKeyScreen(OnboardingScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "prev_provider", "Previous", show=False),
        Binding("down", "next_provider", "Next", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    NEXT_SCREEN = None

    def __init__(self) -> None:
        super().__init__()
        self.config = VibeConfig.model_construct()

        # Only show providers that actually require an API key.
        self.providers = [
            p for p in self.config.providers if (p.api_key_env_var or "").strip()
        ]

        active_model = self.config.get_active_model()
        active_provider = self.config.get_provider_for_model(active_model)

        self._provider_index = 0
        for i, p in enumerate(self.providers):
            if p.name == active_provider.name:
                self._provider_index = i
                break

        # If no providers require keys, onboarding can be completed without input.
        self._no_keys_needed = len(self.providers) == 0

    @property
    def provider(self):
        return self.providers[self._provider_index]

    def _provider_display_name(self, name: str) -> str:
        return name.replace("_", "-")

    def _compose_provider_link(self, provider_name: str) -> ComposeResult:
        if self.provider.name not in PROVIDER_HELP:
            return

        help_url, help_name = PROVIDER_HELP[self.provider.name]
        yield Static(f"Grab your {provider_name} API key from the {help_name}:")
        yield Center(
            Horizontal(
                Static("→ ", classes="link-chevron"),
                Link(help_url, url=help_url),
                classes="link-row",
            )
        )

    def _compose_config_docs(self) -> ComposeResult:
        yield Static("[dim]Learn more about Vibe configuration:[/]")
        yield Horizontal(
            Static("→ ", classes="link-chevron"),
            Link(CONFIG_DOCS_URL, url=CONFIG_DOCS_URL),
            classes="link-row",
        )

    def compose(self) -> ComposeResult:
        if self._no_keys_needed:
            with Vertical(id="api-key-outer"):
                yield Static("", classes="spacer")
                yield Center(Static("Setup complete", id="api-key-title"))
                with Center():
                    with Vertical(id="api-key-content"):
                        yield Static(
                            "No API keys are required for your current configuration.",
                            id="paste-hint",
                        )
                        yield Static("Press ESC to exit.")
                yield Static("", classes="spacer")
                yield Vertical(
                    Vertical(*self._compose_config_docs(), id="config-docs-group"),
                    id="config-docs-section",
                )
            return

        provider_name = self._provider_display_name(self.provider.name).capitalize()

        self.provider_widget = Static("", id="provider")

        self.input_widget = Input(
            password=True,
            id="key",
            placeholder=f"Paste your {provider_name} API key here",
            validators=[Length(minimum=1, failure_description="No API key provided.")],
        )

        self._update_provider_display()

        with Vertical(id="api-key-outer"):
            yield Static("", classes="spacer")
            yield Center(Static("One last thing...", id="api-key-title"))
            with Center():
                with Vertical(id="api-key-content"):
                    yield Static("Select provider ↑ ↓", id="provider-hint")
                    yield Center(self.provider_widget)
                    yield from self._compose_provider_link(provider_name)
                    yield Static(
                        "...and paste it below to finish the setup:", id="paste-hint"
                    )
                    yield Center(Horizontal(self.input_widget, id="input-box"))
                    yield Static("", id="feedback")
            yield Static("", classes="spacer")
            yield Vertical(
                Vertical(*self._compose_config_docs(), id="config-docs-group"),
                id="config-docs-section",
            )

    def on_mount(self) -> None:
        if self._no_keys_needed:
            return
        self.input_widget.focus()

    def _update_provider_display(self) -> None:
        if self._no_keys_needed:
            return

        provider_name = self._provider_display_name(self.provider.name).capitalize()
        self.provider_widget.update(f" {provider_name} ")

        if self.input_widget:
            self.input_widget.placeholder = f"Paste your {provider_name} API key here"
            self.input_widget.value = ""

        # Update help link section by rebuilding the screen.
        # This is simple and OK for onboarding scale.
        self.refresh(recompose=True)

    def action_next_provider(self) -> None:
        if self._no_keys_needed:
            return
        self._provider_index = (self._provider_index + 1) % len(self.providers)
        self._update_provider_display()

    def action_prev_provider(self) -> None:
        if self._no_keys_needed:
            return
        self._provider_index = (self._provider_index - 1) % len(self.providers)
        self._update_provider_display()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._no_keys_needed:
            return

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._no_keys_needed:
            return
        if event.validation_result and event.validation_result.is_valid:
            self._save_and_finish(event.value)

    def _save_and_finish(self, api_key: str) -> None:
        env_key = (self.provider.api_key_env_var or "").strip()
        if not env_key:
            self.app.exit("completed")
            return

        os.environ[env_key] = api_key
        try:
            _save_api_key_to_env_file(env_key, api_key)
        except OSError as err:
            self.app.exit(f"save_error:{err}")
            return
        self.app.exit("completed")

    def on_mouse_up(self, event: MouseUp) -> None:
        copy_selection_to_clipboard(self.app)
