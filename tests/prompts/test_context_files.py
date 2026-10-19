from __future__ import annotations

from pathlib import Path

from vibe.core.prompts.context_files import (
    CONTEXT_FILENAMES,
    TRUNCATION_NOTE,
    ContextFile,
    find_context_file,
    load_context_files,
    render_context_files,
)


def _write(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def _load(
    workdir: Path, agent_dir: Path, per_file: int = 4096, total: int = 16384
) -> list[ContextFile]:
    return load_context_files(
        workdir, agent_dir, max_bytes_per_file=per_file, max_total_bytes=total
    )


class TestContextFilenames:
    def test_override_comes_before_agents(self) -> None:
        assert CONTEXT_FILENAMES.index("AGENTS.override.md") < CONTEXT_FILENAMES.index(
            "AGENTS.md"
        )

    def test_claude_fallback_is_supported(self) -> None:
        assert "CLAUDE.md" in CONTEXT_FILENAMES
        assert "CLAUDE.MD" in CONTEXT_FILENAMES


class TestFindContextFile:
    def test_prefers_override(self, tmp_path: Path) -> None:
        _write(tmp_path, "AGENTS.md", "base")
        override = _write(tmp_path, "AGENTS.override.md", "override")

        assert find_context_file(tmp_path) == override

    def test_falls_back_to_claude(self, tmp_path: Path) -> None:
        claude = _write(tmp_path, "CLAUDE.md", "claude docs")

        assert find_context_file(tmp_path) == claude

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert find_context_file(tmp_path) is None


class TestLoadContextFiles:
    def test_loads_from_ancestors(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "home"
        root = tmp_path / "repo"
        nested = root / "src" / "deep"
        nested.mkdir(parents=True)
        _write(root, "AGENTS.md", "root rules")
        _write(root / "src", "AGENTS.md", "src rules")

        files = _load(nested, agent_dir)

        assert [file.path for file in files] == [
            (root / "src" / "AGENTS.md").resolve(),
            (root / "AGENTS.md").resolve(),
        ]
        assert [file.content for file in files] == ["src rules", "root rules"]

    def test_global_agent_dir_comes_first(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "home"
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _write(agent_dir, "AGENTS.md", "global rules")
        _write(workdir, "AGENTS.md", "project rules")

        files = _load(workdir, agent_dir)

        assert [file.content for file in files] == ["global rules", "project rules"]

    def test_uses_first_matching_name_per_directory(self, tmp_path: Path) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _write(workdir, "AGENTS.md", "agents")
        _write(workdir, "CLAUDE.md", "claude")

        files = _load(workdir, tmp_path / "home")

        assert [file.content for file in files] == ["agents"]

    def test_deduplicates_by_content(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "home"
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _write(agent_dir, "AGENTS.md", "shared")
        _write(workdir, "AGENTS.md", "shared")

        files = _load(workdir, agent_dir)

        assert len(files) == 1
        assert files[0].path == (agent_dir / "AGENTS.md").resolve()

    def test_truncates_per_file_with_note(self, tmp_path: Path) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _write(workdir, "AGENTS.md", "x" * 100)

        files = _load(workdir, tmp_path / "home", per_file=10)

        assert len(files) == 1
        assert files[0].truncated is True
        assert files[0].content == "x" * 10 + TRUNCATION_NOTE

    def test_trims_farthest_files_when_total_exceeds(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        _write(root, "AGENTS.md", "r" * 50)
        _write(root / "a", "AGENTS.md", "a" * 50)
        _write(nested, "AGENTS.md", "b" * 50)

        files = _load(nested, tmp_path / "home", per_file=100, total=120)

        assert len(files) == 3
        assert files[0].content == "b" * 50
        assert files[1].content == "a" * 50
        assert files[2].truncated is True
        assert files[2].content.endswith(TRUNCATION_NOTE)

    def test_drops_farthest_file_when_no_room(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        _write(root, "AGENTS.md", "r" * 50)
        _write(nested, "AGENTS.md", "b" * 50)

        files = _load(nested, tmp_path / "home", per_file=100, total=60)

        assert len(files) == 1
        assert files[0].content == "b" * 50

    def test_skips_missing_directories(self, tmp_path: Path) -> None:
        files = _load(tmp_path / "missing", tmp_path / "home")
        assert files == []


class TestRenderContextFiles:
    def test_renders_project_instructions_tag(self, tmp_path: Path) -> None:
        path = (tmp_path / "AGENTS.md").resolve()
        files = [ContextFile(path=path, content="hello")]

        rendered = render_context_files(files)

        assert rendered == (
            f'<project_instructions path="{path}">\nhello\n</project_instructions>'
        )

    def test_joins_multiple_files_with_blank_line(self, tmp_path: Path) -> None:
        files = [
            ContextFile(path=(tmp_path / "a.md").resolve(), content="a"),
            ContextFile(path=(tmp_path / "b.md").resolve(), content="b"),
        ]

        rendered = render_context_files(files)

        assert rendered.count("<project_instructions") == 2
        assert "</project_instructions>\n\n<project_instructions" in rendered

    def test_empty_list_renders_empty_string(self) -> None:
        assert render_context_files([]) == ""
