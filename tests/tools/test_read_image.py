from __future__ import annotations

from pathlib import Path

import pytest

from tests.backend.test_image_encoding import write_png
from vibe.core.context.tokens import IMAGE_CHARS, estimate_message_tokens
from vibe.core.images import MAX_IMAGE_BYTES, describe_image
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.read_file import (
    ReadFile,
    ReadFileArgs,
    ReadFileState,
    ReadFileToolConfig,
)
from vibe.core.types import LLMMessage, Role


def make_tool(tmp_path: Path) -> ReadFile:
    return ReadFile(config=ReadFileToolConfig(workdir=tmp_path), state=ReadFileState())


@pytest.mark.asyncio
async def test_read_image_returns_a_reference(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    tool = make_tool(tmp_path)

    result = await tool.run(ReadFileArgs(path="pixel.png"))

    assert result.image is not None
    assert result.image.media_type == "image/png"
    assert result.image.source_path == str(path)
    assert result.image.data == ""
    assert result.lines_read == 0
    assert "image/png" in result.content
    assert str(path.resolve()) in tool.state.recently_read_files


@pytest.mark.asyncio
async def test_read_oversized_image_without_pillow_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_png(tmp_path / "huge.png", size=MAX_IMAGE_BYTES + 1)
    monkeypatch.setattr(
        "vibe.core.tools.builtins.read_file.pillow_available", lambda: False
    )
    tool = make_tool(tmp_path)

    with pytest.raises(ToolError, match="Pillow"):
        await tool.run(ReadFileArgs(path="huge.png"))


@pytest.mark.asyncio
async def test_read_text_file_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
    tool = make_tool(tmp_path)

    result = await tool.run(ReadFileArgs(path="notes.txt"))

    assert result.image is None
    assert result.content == "hello\nworld\n"
    assert result.lines_read == 2


def test_estimator_counts_images(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    without = LLMMessage(role=Role.user, content="hi")
    with_image = LLMMessage(role=Role.user, content="hi", images=[describe_image(path)])

    assert estimate_message_tokens(with_image) - estimate_message_tokens(without) == (
        IMAGE_CHARS
    )
