from __future__ import annotations

from pathlib import Path

import pytest

from vibe.cli.commands import Command
from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.chat_input.completion_popup import CompletionPopup
from vibe.core.config import SessionLoggingConfig, VibeConfig


@pytest.fixture
def vibe_config() -> VibeConfig:
    return VibeConfig(session_logging=SessionLoggingConfig(enabled=False))


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    path = tmp_path / "templates"
    path.mkdir()
    (path / "pr.md").write_text(
        "---\ndescription: Create a pull request\n---\n\nOpen a PR.\n", encoding="utf-8"
    )
    return path


@pytest.mark.asyncio
async def test_dynamic_template_commands_appear_in_autocomplete(
    vibe_config: VibeConfig, templates_dir: Path
) -> None:
    app = VibeApp(config=vibe_config, prompt_template_dirs=[templates_dir])

    async with app.run_test() as pilot:
        popup = app.query_one(CompletionPopup)

        await pilot.press(*"/pr")

        assert "/pr" in str(popup.render())
        assert popup.styles.display == "block"


@pytest.mark.asyncio
async def test_refresh_picks_up_newly_registered_commands(
    vibe_config: VibeConfig,
) -> None:
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        popup = app.query_one(CompletionPopup)

        app.commands.register_command(
            "later",
            Command(
                aliases=frozenset({"/later"}),
                description="Run later",
                handler="_show_help",
            ),
        )
        app._refresh_chat_completions()

        await pilot.press(*"/lat")

        assert "/later" in str(popup.render())
        assert popup.styles.display == "block"


@pytest.mark.asyncio
async def test_new_builtin_commands_are_suggested(vibe_config: VibeConfig) -> None:
    app = VibeApp(config=vibe_config)

    async with app.run_test() as pilot:
        popup = app.query_one(CompletionPopup)

        await pilot.press(*"/expo")

        assert "/export" in str(popup.render())
        assert popup.styles.display == "block"
