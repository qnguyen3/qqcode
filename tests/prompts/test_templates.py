from __future__ import annotations

from pathlib import Path

from vibe.core.prompts.templates import (
    PromptTemplate,
    expand,
    load_templates,
    parse_frontmatter,
    render_template,
    split_args,
)


class TestExpandPositional:
    def test_expands_positional_args(self) -> None:
        assert expand("Review $1 and $2", ["src/a.py", "src/b.py"]) == (
            "Review src/a.py and src/b.py"
        )

    def test_missing_positional_becomes_empty(self) -> None:
        assert expand("Hello $1$2", ["world"]) == "Hello world"

    def test_expands_all_args(self) -> None:
        assert expand("Files: $@", ["a", "b", "c"]) == "Files: a b c"

    def test_expands_arguments_keyword(self) -> None:
        assert expand("Do $ARGUMENTS", ["a", "b"]) == "Do a b"

    def test_no_args(self) -> None:
        assert expand("$1|$@", []) == "|"


class TestExpandDefaults:
    def test_default_used_when_arg_missing(self) -> None:
        assert expand("${1:-main}", []) == "main"

    def test_default_used_when_arg_empty(self) -> None:
        assert expand("${1:-main}", [""]) == "main"

    def test_arg_wins_over_default(self) -> None:
        assert expand("${1:-main}", ["dev"]) == "dev"

    def test_all_args_default(self) -> None:
        assert expand("${@:-nothing}", []) == "nothing"
        assert expand("${@:-nothing}", ["a", "b"]) == "a b"


class TestExpandSlices:
    def test_slice_from_position(self) -> None:
        assert expand("${@:2}", ["a", "b", "c"]) == "b c"

    def test_slice_with_length(self) -> None:
        assert expand("${@:2:2}", ["a", "b", "c", "d"]) == "b c"

    def test_slice_beyond_bounds(self) -> None:
        assert expand("${@:3}", ["a"]) == ""
        assert expand("${@:1:9}", ["a", "b"]) == "a b"


class TestSplitArgs:
    def test_double_quotes_group_words(self) -> None:
        assert split_args('"hello world" foo') == ["hello world", "foo"]

    def test_single_quotes_group_words(self) -> None:
        assert split_args("'hello world' foo") == ["hello world", "foo"]

    def test_mixed_quotes(self) -> None:
        assert split_args('a "b c" d') == ["a", "b c", "d"]

    def test_empty_quoted_arg_is_kept(self) -> None:
        assert split_args('"" foo') == ["", "foo"]

    def test_whitespace_only(self) -> None:
        assert split_args("   ") == []


class TestParseFrontmatter:
    def test_parses_description_and_argument_hint(self) -> None:
        content = """---
description: Create a PR
argument-hint: <branch>
---
Body
"""
        frontmatter = parse_frontmatter(content)

        assert frontmatter["description"] == "Create a PR"
        assert frontmatter["argument-hint"] == "<branch>"

    def test_strips_quotes(self) -> None:
        content = '---\ndescription: "quoted"\n---\n'
        assert parse_frontmatter(content)["description"] == "quoted"

    def test_no_frontmatter(self) -> None:
        assert parse_frontmatter("plain body") == {}


class TestLoadTemplates:
    def test_loads_template_with_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "pr.md"
        path.write_text(
            """---
description: Create a pull request
argument-hint: <branch>
---

Review $1
"""
        )

        templates = load_templates([tmp_path])

        assert set(templates) == {"pr"}
        template = templates["pr"]
        assert template.name == "pr"
        assert template.description == "Create a pull request"
        assert template.argument_hint == "<branch>"
        assert template.body.strip() == "Review $1"
        assert template.source == path

    def test_earlier_directory_wins(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        (first / "pr.md").write_text("first body")
        (second / "pr.md").write_text("second body")

        templates = load_templates([first, second])

        assert templates["pr"].body == "first body"

    def test_skips_invalid_names(self, tmp_path: Path) -> None:
        (tmp_path / "Bad Name.md").write_text("body")
        (tmp_path / "good-name.md").write_text("body")

        templates = load_templates([tmp_path])

        assert set(templates) == {"good-name"}

    def test_missing_directory_is_ignored(self, tmp_path: Path) -> None:
        assert load_templates([tmp_path / "missing"]) == {}


class TestRenderTemplate:
    def test_renders_body_with_raw_args(self) -> None:
        template = PromptTemplate(
            name="pr",
            description="",
            body="Create a PR for $1 into ${2:-main}",
            source=Path("pr.md"),
        )

        assert render_template(template, '"feature branch" release') == (
            "Create a PR for feature branch into release"
        )
