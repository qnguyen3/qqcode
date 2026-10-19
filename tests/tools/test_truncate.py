from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from vibe.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationResult,
    format_notice,
    spill_to_temp_file,
    truncate_head,
    truncate_tail,
)


def test_constants_match_spec() -> None:
    assert DEFAULT_MAX_LINES == 2000
    assert DEFAULT_MAX_BYTES == 50 * 1024


def test_truncate_head_keeps_whole_lines() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 11))

    result = truncate_head(text, max_lines=4, max_bytes=10_000)

    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.content == "line 1\nline 2\nline 3\nline 4\n"
    assert result.total_lines == 10
    assert result.first_line == 1
    assert result.last_line == 4


def test_truncate_head_no_truncation_returns_original() -> None:
    text = "a\nb\n"

    result = truncate_head(text, max_lines=10, max_bytes=1_000)

    assert result.truncated is False
    assert result.truncated_by == "none"
    assert result.content == text
    assert result.first_line is None
    assert result.last_line is None
    assert format_notice(result) == ""


def test_truncate_head_byte_limit_never_cuts_a_line() -> None:
    text = "aaaa\nbbbb\ncccc\n"

    result = truncate_head(text, max_lines=100, max_bytes=9)

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.content == "aaaa\n"
    assert result.last_line == 1


def test_truncate_head_single_long_line_falls_back_to_bytes() -> None:
    text = "x" * 100 + "\nsecond\n"

    result = truncate_head(text, max_lines=100, max_bytes=10)

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.content == "x" * 10
    assert result.first_line == 1
    assert result.last_line == 1
    assert "Showing 10 bytes of line 1" in format_notice(result)


def test_truncate_tail_keeps_last_lines() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 11))

    result = truncate_tail(text, max_lines=3, max_bytes=10_000)

    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.content == "line 8\nline 9\nline 10\n"
    assert result.total_lines == 10
    assert result.first_line == 8
    assert result.last_line == 10


def test_truncate_tail_byte_limit_keeps_whole_lines() -> None:
    text = "aaaa\nbbbb\ncccc\n"

    result = truncate_tail(text, max_lines=100, max_bytes=9)

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.content == "cccc\n"
    assert result.first_line == 3
    assert result.last_line == 3


def test_truncate_tail_single_long_line_falls_back_to_bytes() -> None:
    text = "first\n" + "y" * 100

    result = truncate_tail(text, max_lines=100, max_bytes=10)

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.content == "y" * 10
    assert result.first_line == 2
    assert result.last_line == 2


def test_truncate_on_empty_text_is_a_noop() -> None:
    head = truncate_head("", max_lines=10, max_bytes=10)
    tail = truncate_tail("", max_lines=10, max_bytes=10)

    assert not head.truncated
    assert not tail.truncated
    assert head.content == ""
    assert tail.content == ""


def test_spill_to_temp_file_round_trips_and_uses_tmpdir() -> None:
    path = spill_to_temp_file("hello world")

    assert path.exists()
    assert path.read_text("utf-8") == "hello world"
    assert Path(tempfile.gettempdir()) in path.parents

    path.unlink(missing_ok=True)


def test_format_notice_includes_line_range_and_path() -> None:
    path = spill_to_temp_file("payload")
    result = TruncationResult(
        content="payload",
        truncated=True,
        truncated_by="lines",
        total_lines=10,
        first_line=2,
        last_line=4,
        full_output_path=path,
    )

    assert format_notice(result) == (f"[Showing lines 2-4 of 10. Full output: {path}]")

    path.unlink(missing_ok=True)


def test_format_notice_without_path() -> None:
    result = TruncationResult(
        content="b\nc\n",
        truncated=True,
        truncated_by="lines",
        total_lines=5,
        first_line=4,
        last_line=5,
    )

    assert format_notice(result) == "[Showing lines 4-5 of 5]"


@pytest.mark.asyncio
async def test_read_file_uses_head_truncation_with_offset_notice(tmp_path) -> None:
    from vibe.core.tools.builtins.read_file import (
        ReadFile,
        ReadFileArgs,
        ReadFileState,
        ReadFileToolConfig,
    )

    target = tmp_path / "big.txt"
    target.write_text("".join(f"line {i}\n" for i in range(1, 201)))
    config = ReadFileToolConfig(workdir=tmp_path, max_read_bytes=50)
    tool = ReadFile(config=config, state=ReadFileState())

    result = await tool.run(ReadFileArgs(path="big.txt"))

    assert result.truncated is True
    assert result.notice is not None
    assert "Use offset=" in result.notice
    assert result.lines_read == len(result.content.splitlines())
    assert "line 1\n" == result.content or result.content.startswith("line 1\n")
    assert not result.content.endswith("line")

    next_offset = result.lines_read
    continuation = await tool.run(ReadFileArgs(path="big.txt", offset=next_offset))
    assert continuation.content.startswith(f"line {next_offset + 1}\n")
