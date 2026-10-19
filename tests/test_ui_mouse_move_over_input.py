from __future__ import annotations

import pytest
from textual.geometry import Region

from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.chat_input.text_area import ChatTextArea
from vibe.core.config import SessionLoggingConfig, VibeConfig


@pytest.mark.asyncio
async def test_input_renders_when_ancestors_are_transparent() -> None:
    config = VibeConfig(session_logging=SessionLoggingConfig(enabled=False))
    app = VibeApp(config=config)

    async with app.run_test() as pilot:
        text_area = app.query_one(ChatTextArea)
        for ancestor in text_area.ancestors:
            ancestor.styles.background = "transparent"
        await pilot.pause()

        assert text_area.rich_style.bgcolor is not None
        assert len(text_area.render_lines(Region(0, 0, 40, 3))) == 3
