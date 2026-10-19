from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from vibe.core.tools.base import BaseToolState, ToolError
from vibe.core.tools.builtins.find import Find, FindArgs, FindConfig
from vibe.core.tools.builtins.ls import Ls, LsArgs, LsConfig


@pytest.fixture
def find_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Find:
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda cmd: None if cmd == "fd" else original_which(cmd)
    )
    return Find(config=FindConfig(workdir=tmp_path), state=BaseToolState())


@pytest.fixture
def ls_tool(tmp_path: Path) -> Ls:
    return Ls(config=LsConfig(workdir=tmp_path), state=BaseToolState())


@pytest.mark.asyncio
async def test_find_matches_glob_pattern(find_tool: Find, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c")

    result = await find_tool.run(FindArgs(pattern="*.py"))

    assert result.truncated is False
    assert result.match_count == 2
    assert set(result.matches.splitlines()) == {"a.py", "sub/c.py"}


@pytest.mark.asyncio
async def test_find_respects_path_argument(find_tool: Find, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c")

    result = await find_tool.run(FindArgs(pattern="*.py", path="sub"))

    assert result.matches.splitlines() == ["c.py"]


@pytest.mark.asyncio
async def test_find_applies_limit(find_tool: Find, tmp_path: Path) -> None:
    for index in range(10):
        (tmp_path / f"file_{index}.txt").write_text("x")

    result = await find_tool.run(FindArgs(pattern="*.txt", limit=4))

    assert result.match_count == 4
    assert result.truncated is True
    assert result.notice is not None
    assert "Showing lines 1-4" in result.notice


@pytest.mark.asyncio
async def test_find_respects_gitignore(find_tool: Find, tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "included.py").write_text("x")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("x")

    result = await find_tool.run(FindArgs(pattern="*.py"))

    assert "included.py" in result.matches
    assert "hidden.py" not in result.matches
    assert "ignored" not in result.matches


@pytest.mark.asyncio
async def test_find_missing_path_is_an_error(find_tool: Find) -> None:
    with pytest.raises(ToolError):
        await find_tool.run(FindArgs(pattern="*.py", path="missing"))


@pytest.mark.asyncio
async def test_ls_sorts_and_marks_directories(ls_tool: Ls, tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / ".hidden").write_text("h")
    (tmp_path / "a").mkdir()
    (tmp_path / "C.txt").write_text("c")

    result = await ls_tool.run(LsArgs())

    assert result.entry_count == 4
    assert result.entries.splitlines() == [".hidden", "a/", "b.txt", "C.txt"]


@pytest.mark.asyncio
async def test_ls_applies_limit(ls_tool: Ls, tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"file_{index:02d}.txt").write_text("x")

    result = await ls_tool.run(LsArgs(limit=5))

    assert result.entry_count == 5
    assert result.truncated is True
    assert result.notice is not None
    assert result.entries.splitlines() == [
        "file_00.txt",
        "file_01.txt",
        "file_02.txt",
        "file_03.txt",
        "file_04.txt",
    ]


@pytest.mark.asyncio
async def test_ls_missing_path_is_an_error(ls_tool: Ls) -> None:
    with pytest.raises(ToolError):
        await ls_tool.run(LsArgs(path="missing"))
