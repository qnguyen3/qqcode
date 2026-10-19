from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import sys
from typing import Any, ClassVar
import webbrowser

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vibe.core.autocompletion.fuzzy import fuzzy_match
from vibe.core.oauth.base import DeviceCodeInfo, SelectOption

SELECTED_CLASS = "login-option-selected"
DIALOG_CLASS = "login-dialog"
TITLE_CLASS = "login-title"
SEARCH_CLASS = "login-search"
LIST_CLASS = "login-list"
EMPTY_CLASS = "login-empty"
HELP_CLASS = "login-help"
SECRET_CLASS = "login-secret"
BODY_CLASS = "login-body"
STATUS_CLASS = "login-status"
INPUT_CLASS = "login-input"


@dataclass(frozen=True)
class SelectorEntry:
    """One selectable row: a provider, an auth method or a credential."""

    label: str
    value: Any = None
    detail: str = ""
    configured: bool = False

    def searchable_text(self) -> str:
        return f"{self.label} {self.detail}"


class LoginScreen(ModalScreen[Any]):
    """Modal selector with fuzzy search, used for login and logout flows."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, entries: list[SelectorEntry]) -> None:
        super().__init__()
        self.title_text = title
        self.entries = entries
        self.filtered = list(entries)
        self.selected_index = 0
        self.rows: list[tuple[SelectorEntry, Static]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes=DIALOG_CLASS):
            yield Static(self.title_text, classes=TITLE_CLASS)
            yield Input(placeholder="Search…", classes=SEARCH_CLASS)
            with VerticalScroll(classes=LIST_CLASS):
                for entry in self.entries:
                    widget = Static("", classes="login-option")
                    self.rows.append((entry, widget))
                    yield widget
                yield Static("  No matches.", classes=EMPTY_CLASS)
            yield Static("↑↓ navigate  Enter select  Esc cancel", classes=HELP_CLASS)

    def on_mount(self) -> None:
        self._update_display()
        self.query_one(f".{SEARCH_CLASS}", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_select()

    def _apply_filter(self, pattern: str) -> None:
        pattern = pattern.strip()
        scored = [
            (match.score, entry)
            for entry in self.entries
            if (match := fuzzy_match(pattern, entry.searchable_text())).matched
        ]
        self.filtered = [
            entry for _, entry in sorted(scored, key=lambda item: item[0], reverse=True)
        ]
        self.selected_index = 0
        self._update_display()

    def _update_display(self) -> None:
        visible = {id(entry) for entry in self.filtered}
        selected = self.filtered[self.selected_index] if self.filtered else None
        selected_widget: Static | None = None
        for entry, widget in self.rows:
            is_selected = entry is selected
            widget.display = id(entry) in visible
            widget.update(self._format_entry(entry, selected=is_selected))
            widget.set_class(is_selected, SELECTED_CLASS)
            if is_selected:
                selected_widget = widget
        if selected_widget is not None:
            selected_widget.scroll_visible(animate=False)
        self.query_one(f".{EMPTY_CLASS}", Static).display = not self.filtered

    @staticmethod
    def _format_entry(entry: SelectorEntry, *, selected: bool) -> str:
        cursor = "›" if selected else " "
        detail = f"  [dim]{entry.detail}[/dim]" if entry.detail else ""
        return f"{cursor} {entry.label}{detail}"

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
        self.dismiss(self.filtered[self.selected_index].value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SecretScreen(ModalScreen[str | None]):
    """Modal asking for a secret (an API key), masked while typing."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes=DIALOG_CLASS):
            yield Static(self.title_text, classes=TITLE_CLASS)
            yield Input(
                password=True, placeholder=self.placeholder, classes=SECRET_CLASS
            )
            yield Static("Enter save  Esc cancel", classes=HELP_CLASS)

    def on_mount(self) -> None:
        self.query_one(f".{SECRET_CLASS}", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LoginCancelledError(Exception):
    """Raised inside a login flow when the user cancels the dialog."""


@dataclass(frozen=True)
class LoginResult:
    """What a modal login reports back to its caller."""

    success: bool
    message: str | None = None
    cancelled: bool = False


class LoginDialogScreen(ModalScreen[LoginResult]):
    """Modal login dialog implementing the OAuth ``AuthInteraction`` surface.

    A login flow drives the dialog from a worker: ``show_auth_url`` and
    ``show_device_code`` fill the body, ``progress`` updates the status line and
    the prompt methods reuse a single input row, masked for secrets. Escape sets
    the interaction's cancel event so the flow aborts, then dismisses.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, provider_name: str) -> None:
        super().__init__()
        self.provider_name = provider_name
        self.cancel_event = asyncio.Event()
        self._pending: asyncio.Future[str] | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def compose(self) -> ComposeResult:
        with Vertical(classes=DIALOG_CLASS):
            yield Static(f"Login to {self.provider_name}", classes=TITLE_CLASS)
            yield Vertical(classes=BODY_CLASS)
            yield Static("", classes=STATUS_CLASS)
            yield Input(classes=INPUT_CLASS)
            yield Static("Enter submit  Esc cancel", classes=HELP_CLASS)

    def on_mount(self) -> None:
        self._input().display = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_result(event.value)

    async def show_auth_url(self, url: str, instructions: str | None = None) -> None:
        if self.cancelled:
            return
        await self._clear_body()
        await self._append_body(f'[link="{url}"]{url}[/link]')
        await self._append_body(f"[dim]{self._click_hint()}[/dim]")
        if instructions:
            await self._append_body(f"[yellow]{instructions}[/yellow]")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    async def show_device_code(self, info: DeviceCodeInfo) -> None:
        if self.cancelled:
            return
        await self._clear_body()
        await self._append_body(
            f'[link="{info.verification_uri}"]{info.verification_uri}[/link]'
        )
        await self._append_body(f"[dim]{self._click_hint()}[/dim]")
        await self._append_body(f"[yellow]Enter code: {info.user_code}[/yellow]")
        self._set_status("Waiting for authentication…")

    async def progress(self, message: str) -> None:
        if self.cancelled:
            return
        self._set_status(message)

    async def prompt_secret(self, message: str) -> str:
        return await self._prompt(message, masked=True, placeholder=None)

    async def prompt_text(self, message: str, placeholder: str | None = None) -> str:
        return await self._prompt(message, masked=False, placeholder=placeholder)

    async def select(self, message: str, options: Sequence[SelectOption]) -> str:
        await self._append_body(f"[bold]{message}[/bold]")
        for index, option in enumerate(options, start=1):
            detail = f"  [dim]{option.description}[/dim]" if option.description else ""
            await self._append_body(f"  {index}. {option.label}{detail}")
        while True:
            answer = (await self._ask(masked=False, placeholder=None)).strip()
            match self._match_option(answer, options):
                case SelectOption() as option:
                    await self._append_body(f"> {option.id}")
                    return option.id
                case None:
                    self._set_status("Enter one of the numbers above.")

    def action_cancel(self) -> None:
        self.cancel_event.set()
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(LoginCancelledError("Login cancelled"))
        self.dismiss(
            LoginResult(success=False, message="Login cancelled", cancelled=True)
        )

    async def _prompt(
        self, message: str, *, masked: bool, placeholder: str | None
    ) -> str:
        await self._append_body(f"[bold]{message}[/bold]")
        if placeholder:
            await self._append_body(f"[dim]e.g., {placeholder}[/dim]")
        return await self._ask(masked=masked, placeholder=placeholder)

    async def _ask(self, *, masked: bool, placeholder: str | None) -> str:
        if self.cancelled:
            raise LoginCancelledError("Login cancelled")
        field = self._input()
        field.password = masked
        field.placeholder = placeholder or ""
        field.value = ""
        field.display = True
        field.focus()
        self._pending = asyncio.get_running_loop().create_future()
        try:
            value = await self._pending
        finally:
            self._pending = None
            if field.is_mounted:
                field.display = False
        await self._append_body(f"> {'•' * len(value) if masked else value}")
        return value

    @staticmethod
    def _match_option(
        answer: str, options: Sequence[SelectOption]
    ) -> SelectOption | None:
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return next((option for option in options if option.id == answer), None)

    @staticmethod
    def _click_hint() -> str:
        return "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"

    def _input(self) -> Input:
        return self.query_one(f".{INPUT_CLASS}", Input)

    async def _clear_body(self) -> None:
        if self.is_mounted:
            await self.query_one(f".{BODY_CLASS}", Vertical).remove_children()

    async def _append_body(self, line: str) -> None:
        if self.is_mounted:
            await self.query_one(f".{BODY_CLASS}", Vertical).mount(Static(line))

    def _set_status(self, message: str) -> None:
        if self.is_mounted:
            self.query_one(f".{STATUS_CLASS}", Static).update(message)
