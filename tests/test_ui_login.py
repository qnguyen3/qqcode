from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from textual.containers import Vertical
from textual.widgets import Static

from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.login_screen import (
    BODY_CLASS,
    STATUS_CLASS,
    LoginDialogScreen,
    LoginScreen,
    SecretScreen,
)
from vibe.cli.textual_ui.widgets.messages import UserCommandMessage
from vibe.core.config import Backend, ProviderConfig, SessionLoggingConfig, VibeConfig
from vibe.core.oauth import OAuthToken, get_flow, register_flow
from vibe.core.oauth.base import DeviceCodeInfo


@pytest.fixture
def vibe_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        workdir=tmp_path,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
        providers=[
            ProviderConfig(
                name="mistral",
                api_base="https://api.mistral.ai/v1",
                api_key_env_var="MISTRAL_API_KEY",
                backend=Backend.MISTRAL,
            ),
            ProviderConfig(
                name="anthropic",
                api_base="https://api.anthropic.com",
                api_key_env_var="ANTHROPIC_API_KEY",
                api_style="anthropic",
            ),
        ],
    )


class FakeOAuthFlow:
    """Scripted OAuth flow that records the interaction it was given."""

    name: ClassVar[str] = "anthropic"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with Claude"

    def __init__(self, script: Callable[[Any], Awaitable[OAuthToken]]) -> None:
        self.script = script
        self.interaction: Any = None
        self.observed_cancel = False

    async def login(self, interaction: Any) -> OAuthToken:
        self.interaction = interaction
        try:
            return await self.script(interaction)
        finally:
            self.observed_cancel = interaction.cancelled

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        raise NotImplementedError


InstallFakeFlow = Callable[[Callable[[Any], Awaitable[OAuthToken]]], FakeOAuthFlow]


@pytest.fixture
def fake_flow() -> Iterator[InstallFakeFlow]:
    original = get_flow("anthropic")
    yield _install
    register_flow(original)


def _install(script: Callable[[Any], Awaitable[OAuthToken]]) -> FakeOAuthFlow:
    flow = FakeOAuthFlow(script)
    register_flow(flow)
    return flow


def _token() -> OAuthToken:
    return OAuthToken(
        access_token="access",
        refresh_token="refresh",
        expires_in=3600,
        expires_at=9999999999,
    )


def _noop_save(provider_name: str, token: OAuthToken) -> None:
    return None


async def _noop() -> None:
    return None


def _reload_recorder(calls: list[bool]) -> Callable[[], Awaitable[None]]:
    async def record() -> None:
        calls.append(True)

    return record


def _stub_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: True)


def _body_text(screen: LoginDialogScreen) -> str:
    body = screen.query_one(f".{BODY_CLASS}", Vertical)
    return "\n".join(str(child.render()) for child in body.children)


async def type_and_submit(pilot: Any, text: str) -> None:
    await pilot.press(*text)
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_login_flow_stores_an_api_key(
    vibe_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "vibe.cli.textual_ui.app.save_api_key",
        lambda provider, key: saved.append((provider.name, key)) or Path("/tmp/.env"),
    )
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login")

        assert isinstance(app.screen, LoginScreen)
        await type_and_submit(pilot, "api key")

        assert isinstance(app.screen, LoginScreen)
        await type_and_submit(pilot, "mistral")

        assert isinstance(app.screen, SecretScreen)
        await type_and_submit(pilot, "sk-test-key")

    assert saved == [("mistral", "sk-test-key")]


@pytest.mark.asyncio
async def test_login_with_provider_argument_skips_selection(
    vibe_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[str] = []
    monkeypatch.setattr(
        "vibe.cli.textual_ui.app.save_api_key",
        lambda provider, key: saved.append(key) or Path("/tmp/.env"),
    )
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login mistral")

        assert isinstance(app.screen, SecretScreen)
        await type_and_submit(pilot, "sk-direct")

    assert saved == ["sk-direct"]


@pytest.mark.asyncio
async def test_login_offers_subscription_for_oauth_provider(
    vibe_config: VibeConfig, fake_flow: InstallFakeFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(interaction: Any) -> OAuthToken:
        await interaction.show_auth_url("https://anthropic.test/auth")
        return _token()

    flow = fake_flow(script)
    _stub_browser(monkeypatch)
    monkeypatch.setattr("vibe.cli.textual_ui.app.save_oauth_token", _noop_save)
    app = VibeApp(config=vibe_config)
    monkeypatch.setattr(app, "_reload_config", _noop)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login anthropic")

        assert isinstance(app.screen, LoginScreen)
        await type_and_submit(pilot, "subscription")
        await pilot.pause(0.1)

        assert not isinstance(app.screen, LoginScreen)

    assert isinstance(flow.interaction, LoginDialogScreen)


@pytest.mark.asyncio
async def test_login_reports_unknown_provider(vibe_config: VibeConfig) -> None:
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login nope")

        assert not isinstance(app.screen, LoginScreen)


@pytest.mark.asyncio
async def test_login_ambient_provider_shows_setup_without_secret(
    vibe_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_GATEWAY_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login cloudflare-workers-ai")
        await pilot.pause()

        assert not isinstance(app.screen, SecretScreen)
        assert not isinstance(app.screen, LoginScreen)
        messages = [message._content for message in app.query(UserCommandMessage)]

    assert any("ambient credentials" in str(message) for message in messages)


@pytest.mark.asyncio
async def test_logout_removes_a_configured_credential(
    vibe_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-test-key")
    removed: list[str] = []
    monkeypatch.setattr(
        "vibe.cli.textual_ui.app.logout",
        lambda config, name: removed.append(name) or [f"removed {name}"],
    )
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/logout")

        assert isinstance(app.screen, LoginScreen)
        await type_and_submit(pilot, "mistral")

    assert removed == ["mistral"]


async def _open_subscription_dialog(pilot: Any) -> None:
    await type_and_submit(pilot, "/login anthropic")
    await type_and_submit(pilot, "subscription")
    await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_oauth_dialog_shows_auth_url_then_reports_success(
    vibe_config: VibeConfig, fake_flow: InstallFakeFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def script(interaction: Any) -> OAuthToken:
        await interaction.show_auth_url(
            "https://anthropic.test/auth", "Paste the code Anthropic shows."
        )
        await release.wait()
        return _token()

    fake_flow(script)
    _stub_browser(monkeypatch)
    monkeypatch.setattr("vibe.cli.textual_ui.app.save_oauth_token", _noop_save)
    app = VibeApp(config=vibe_config)
    monkeypatch.setattr(app, "_reload_config", _noop)

    async with app.run_test() as pilot:
        await _open_subscription_dialog(pilot)

        assert isinstance(app.screen, LoginDialogScreen)
        body = _body_text(app.screen)
        assert "https://anthropic.test/auth" in body
        assert "click to open" in body
        assert "Paste the code Anthropic shows." in body

        release.set()
        await pilot.pause(0.1)

        assert not isinstance(app.screen, LoginDialogScreen)
        messages = [message._content for message in app.query(UserCommandMessage)]

    assert "Signed in to anthropic" in " ".join(messages)


@pytest.mark.asyncio
async def test_oauth_dialog_renders_device_code(
    vibe_config: VibeConfig, fake_flow: InstallFakeFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def script(interaction: Any) -> OAuthToken:
        await interaction.show_device_code(
            DeviceCodeInfo(
                user_code="ABCD-1234", verification_uri="https://anthropic.test/device"
            )
        )
        await release.wait()
        return _token()

    fake_flow(script)
    monkeypatch.setattr("vibe.cli.textual_ui.app.save_oauth_token", _noop_save)
    app = VibeApp(config=vibe_config)
    monkeypatch.setattr(app, "_reload_config", _noop)

    async with app.run_test() as pilot:
        await _open_subscription_dialog(pilot)

        assert isinstance(app.screen, LoginDialogScreen)
        body = _body_text(app.screen)
        assert "https://anthropic.test/device" in body
        assert "Enter code: ABCD-1234" in body
        status = app.screen.query_one(f".{STATUS_CLASS}", Static)
        assert "Waiting for authentication" in str(status.render())

        release.set()
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_oauth_dialog_cancel_returns_to_provider_selector(
    vibe_config: VibeConfig, fake_flow: InstallFakeFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(interaction: Any) -> OAuthToken:
        await interaction.show_auth_url("https://anthropic.test/auth")
        await interaction.cancel_event.wait()
        raise RuntimeError("Login cancelled")

    flow = fake_flow(script)
    _stub_browser(monkeypatch)
    app = VibeApp(config=vibe_config)
    monkeypatch.setattr(app, "_reload_config", _noop)

    async with app.run_test() as pilot:
        await type_and_submit(pilot, "/login")
        await type_and_submit(pilot, "subscription")

        assert isinstance(app.screen, LoginScreen)
        assert app.screen.title_text == "Select provider:"
        await type_and_submit(pilot, "anthropic")
        await pilot.pause(0.1)

        assert isinstance(app.screen, LoginDialogScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)

        assert flow.observed_cancel is True
        assert isinstance(app.screen, LoginScreen)
        assert app.screen.title_text == "Select provider:"


@pytest.mark.asyncio
async def test_oauth_dialog_completion_reloads_config(
    vibe_config: VibeConfig, fake_flow: InstallFakeFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(interaction: Any) -> OAuthToken:
        await interaction.show_auth_url("https://anthropic.test/auth")
        return _token()

    fake_flow(script)
    _stub_browser(monkeypatch)
    monkeypatch.setattr("vibe.cli.textual_ui.app.save_oauth_token", _noop_save)
    reloaded: list[bool] = []
    app = VibeApp(config=vibe_config)
    monkeypatch.setattr(app, "_reload_config", _reload_recorder(reloaded))

    async with app.run_test() as pilot:
        await _open_subscription_dialog(pilot)

    assert reloaded == [True]
