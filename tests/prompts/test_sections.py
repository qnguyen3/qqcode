from __future__ import annotations

import pytest

from vibe.core.prompts.sections import SECTION_ORDER, PromptSection, PromptSections


class TestPromptSection:
    def test_render_wraps_tagged_section(self) -> None:
        section = PromptSection(name="preamble", content="Hello")
        assert section.render() == "<preamble>\nHello\n</preamble>"

    def test_render_keeps_untagged_content_raw(self) -> None:
        section = PromptSection(name="preamble", content="Hello", tagged=False)
        assert section.render() == "Hello"

    def test_defaults(self) -> None:
        section = PromptSection(name="mode", content="x")
        assert section.tagged is True
        assert section.cache_stable is True

    @pytest.mark.parametrize(
        "name", ["UPPER", "1section", "with space", "with.dot", "", "-leading"]
    )
    def test_rejects_invalid_names(self, name: str) -> None:
        with pytest.raises(ValueError):
            PromptSection(name=name, content="x")

    @pytest.mark.parametrize(
        "name", ["preamble", "commit_signature", "os_env", "a1-b_c"]
    )
    def test_accepts_valid_names(self, name: str) -> None:
        assert PromptSection(name=name, content="x").name == name


class TestPromptSections:
    def test_render_uses_canonical_order_regardless_of_insertion(self) -> None:
        sections = PromptSections()
        sections.set(PromptSection(name="os_env", content="os", tagged=False))
        sections.set(PromptSection(name="preamble", content="pre", tagged=False))
        sections.set(PromptSection(name="mode", content="plan", tagged=False))

        assert sections.names() == ["preamble", "mode", "os_env"]
        assert sections.render() == "pre\n\nplan\n\nos"

    def test_unknown_sections_render_after_known_ones(self) -> None:
        sections = PromptSections()
        sections.set(
            PromptSection(name="additional_context", content="extra", tagged=False)
        )
        sections.set(PromptSection(name="append", content="tail", tagged=False))
        sections.set(PromptSection(name="preamble", content="pre", tagged=False))

        assert sections.names() == ["preamble", "append", "additional_context"]
        assert sections.render() == "pre\n\ntail\n\nextra"

    def test_canonical_order_constant(self) -> None:
        assert SECTION_ORDER == (
            "preamble",
            "commit_signature",
            "tool_prompts",
            "skills",
            "model_info",
            "docs_routing",
            "project_context",
            "project_files",
            "instructions",
            "mode",
            "os_env",
            "append",
        )

    def test_set_replaces_existing_content(self) -> None:
        sections = PromptSections()
        sections.set(PromptSection(name="mode", content="first", tagged=False))
        sections.set(PromptSection(name="mode", content="second", tagged=False))

        assert len(sections) == 1
        assert sections.render() == "second"

    def test_remove_deletes_section(self) -> None:
        sections = PromptSections()
        sections.set(PromptSection(name="mode", content="plan", tagged=False))
        sections.remove("mode")

        assert "mode" not in sections
        assert sections.get("mode") is None
        assert sections.render() == ""

    def test_remove_missing_section_is_noop(self) -> None:
        sections = PromptSections()
        sections.remove("mode")
        assert len(sections) == 0

    def test_render_skips_empty_sections(self) -> None:
        sections = PromptSections()
        sections.set(PromptSection(name="preamble", content="pre", tagged=False))
        sections.set(PromptSection(name="mode", content="   ", tagged=False))

        assert sections.render() == "pre"

    def test_render_joins_with_blank_lines(self) -> None:
        sections = PromptSections()
        sections.set(PromptSection(name="preamble", content="a", tagged=False))
        sections.set(PromptSection(name="os_env", content="b", tagged=False))

        assert sections.render() == "a\n\nb"

    def test_render_is_deterministic(self) -> None:
        def build() -> str:
            sections = PromptSections()
            sections.set(PromptSection(name="os_env", content="os", tagged=False))
            sections.set(PromptSection(name="preamble", content="pre", tagged=False))
            sections.set(PromptSection(name="append", content="tail", tagged=False))
            return sections.render()

        assert build() == build()

    def test_diff_returns_only_changed_sections(self) -> None:
        before = PromptSections([
            PromptSection(name="preamble", content="same", tagged=False),
            PromptSection(name="mode", content="old", tagged=False),
            PromptSection(name="append", content="gone", tagged=False),
        ])
        after = PromptSections([
            PromptSection(name="preamble", content="same", tagged=False),
            PromptSection(name="mode", content="new", tagged=False),
            PromptSection(name="os_env", content="added", tagged=False),
        ])

        assert before.diff(after) == {"mode": "new", "append": None, "os_env": "added"}

    def test_diff_is_empty_for_equal_sections(self) -> None:
        sections = PromptSections([
            PromptSection(name="preamble", content="same", tagged=False)
        ])
        assert sections.diff(PromptSections(sections)) == {}

    def test_init_accepts_iterable(self) -> None:
        sections = PromptSections([
            PromptSection(name="preamble", content="pre", tagged=False)
        ])
        assert sections.get("preamble") is not None
