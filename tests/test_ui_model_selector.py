from __future__ import annotations

from pathlib import Path
import time

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input

from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.model_selector import ModelSelectorScreen
from vibe.core.config import (
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    VibeConfig,
)

SEARCH = ".model-search"
LIST = ".model-list"


def _model(name: str, provider: str = "opencode-go") -> ModelConfig:
    return ModelConfig(name=name, provider=provider, alias=f"{provider}/{name}")


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VibeConfig:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / ".qqcode-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("vibe.core.auth.GLOBAL_ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-test")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    return VibeConfig(
        workdir=tmp_path,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
        active_model="opencode-go/kimi-k3",
        providers=[
            ProviderConfig(
                name="opencode-go",
                api_base="https://opencode.ai/zen/go/v1",
                api_key_env_var="OPENCODE_API_KEY",
            ),
            ProviderConfig(
                name="mistral",
                api_base="https://api.mistral.ai/v1",
                api_key_env_var="MISTRAL_API_KEY",
            ),
        ],
        models=[
            _model("deepseek-v4.1-flash"),
            _model("kimi-k3"),
            _model("devstral", provider="mistral"),
        ],
    )


async def _submit(pilot, command: str) -> None:
    await pilot.press(*command)
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for_screen(pilot, app, timeout: float = 3.0) -> ModelSelectorScreen:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if isinstance(app.screen, ModelSelectorScreen):
            return app.screen
        await pilot.pause(0.05)
    raise AssertionError("model selector did not open")


@pytest.mark.asyncio
async def test_model_selector_only_lists_signed_in_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    app = VibeApp(config=config)

    async with app.run_test() as pilot:
        await _submit(pilot, "/model")

        screen = await _wait_for_screen(pilot, app)
        aliases = {model.alias for model in screen.models}
        assert "opencode-go/deepseek-v4.1-flash" in aliases
        assert "opencode-go/kimi-k3" in aliases
        # Mistral was not signed in, so none of its models are offered.
        assert all(model.provider != "mistral" for model in screen.models)
        # The picker starts on the active model.
        assert screen.filtered[screen.selected_index].alias == "opencode-go/kimi-k3"


@pytest.mark.asyncio
async def test_model_selector_search_filters_and_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    app = VibeApp(config=config)

    async with app.run_test() as pilot:
        await _submit(pilot, "/model")
        screen = await _wait_for_screen(pilot, app)

        search = screen.query_one(SEARCH, Input)
        # opencode and opencode-go share OPENCODE_API_KEY, so both list a
        # deepseek-v4.1-flash; the provider prefix picks the one under test.
        search.value = "opencode-go/deepseek-v4.1"
        await pilot.pause()

        assert screen.filtered[0].alias == "opencode-go/deepseek-v4.1-flash"

        await pilot.press("enter")
        await pilot.pause()

    assert config.active_model == "opencode-go/deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_model_selector_scrolls_selected_into_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.models = [_model(f"model-{index:02d}") for index in range(40)]
    config.active_model = "opencode-go/model-00"
    app = VibeApp(config=config)

    async with app.run_test() as pilot:
        await _submit(pilot, "/model")
        screen = await _wait_for_screen(pilot, app)

        scroll = screen.query_one(LIST, VerticalScroll)
        assert scroll.scroll_y == 0

        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()

        assert screen.selected_index == 20
        assert scroll.scroll_y > 0


@pytest.mark.asyncio
async def test_model_selector_hints_when_no_provider_is_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    # Only an unsigned-in provider's model remains, so the list is empty.
    config.models = [ModelConfig(name="devstral", provider="mistral", alias="devstral")]
    config.active_model = "devstral"
    app = VibeApp(config=config)

    async with app.run_test() as pilot:
        await _submit(pilot, "/model")
        screen = await _wait_for_screen(pilot, app)

        assert screen.models == []
        assert screen.hint is not None
