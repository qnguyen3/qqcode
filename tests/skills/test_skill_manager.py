from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibe.core.skills.manager import (
    SkillManager,
    SkillNotFoundError,
)


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    """Create a mock VibeConfig with a temporary workdir."""
    config = MagicMock()
    config.effective_workdir = tmp_path
    return config


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a skills directory structure."""
    skills_path = tmp_path / ".qqcode" / "skills"
    skills_path.mkdir(parents=True)
    return skills_path


class TestFrontmatterParsing:
    """Tests for YAML frontmatter parsing."""

    def test_parses_simple_frontmatter(self) -> None:
        content = """---
name: test-skill
description: A test skill for testing.
---

# Test Skill

This is the content.
"""
        result = SkillManager._parse_frontmatter(content)
        assert result["name"] == "test-skill"
        assert result["description"] == "A test skill for testing."

    def test_parses_quoted_values(self) -> None:
        content = """---
name: "quoted-name"
description: 'Single quoted description'
---
"""
        result = SkillManager._parse_frontmatter(content)
        assert result["name"] == "quoted-name"
        assert result["description"] == "Single quoted description"

    def test_returns_empty_dict_for_no_frontmatter(self) -> None:
        content = "# No frontmatter\n\nJust content."
        result = SkillManager._parse_frontmatter(content)
        assert result == {}

    def test_handles_extra_fields(self) -> None:
        content = """---
name: skill-with-extras
description: Has extra fields.
license: MIT
author: Test Author
---
"""
        result = SkillManager._parse_frontmatter(content)
        assert result["name"] == "skill-with-extras"
        assert result["description"] == "Has extra fields."
        assert result["license"] == "MIT"
        assert result["author"] == "Test Author"


class TestSkillDiscovery:
    """Tests for skill discovery."""

    def test_discovers_skill_from_project_directory(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        # Create a skill
        skill_path = skills_dir / "my-skill"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: my-skill
description: A project-local skill.
---

# My Skill

Instructions here.
"""
        )

        manager = SkillManager(mock_config)
        skills = manager.get_available_skills()

        assert len(skills) == 1
        assert skills[0].name == "my-skill"
        assert skills[0].description == "A project-local skill."

    def test_skips_directory_without_skill_md(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        # Create a directory without SKILL.md
        (skills_dir / "not-a-skill").mkdir()

        manager = SkillManager(mock_config)
        assert len(manager.get_available_skills()) == 0

    def test_skips_skill_without_name(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "nameless"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
description: A skill without a name.
---
"""
        )

        manager = SkillManager(mock_config)
        assert len(manager.get_available_skills()) == 0

    def test_skips_skill_without_description(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "no-desc"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: no-desc
---
"""
        )

        manager = SkillManager(mock_config)
        assert len(manager.get_available_skills()) == 0

    def test_discovers_multiple_skills(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        for skill_name in ["skill-a", "skill-b", "skill-c"]:
            skill_path = skills_dir / skill_name
            skill_path.mkdir()
            (skill_path / "SKILL.md").write_text(
                f"""---
name: {skill_name}
description: Description for {skill_name}.
---
"""
            )

        manager = SkillManager(mock_config)
        skills = manager.get_available_skills()

        assert len(skills) == 3
        skill_names = {s.name for s in skills}
        assert skill_names == {"skill-a", "skill-b", "skill-c"}


class TestSkillContent:
    """Tests for loading skill content."""

    def test_gets_skill_content(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "content-test"
        skill_path.mkdir()
        content = """---
name: content-test
description: Test skill content loading.
---

# Content Test Skill

This is the full content of the skill.

## Section 1

Some instructions.

## Section 2

More instructions.
"""
        (skill_path / "SKILL.md").write_text(content)

        manager = SkillManager(mock_config)
        loaded_content = manager.get_skill_content("content-test")

        assert loaded_content == content

    def test_raises_error_for_unknown_skill(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        # Create one skill
        skill_path = skills_dir / "known"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: known
description: A known skill.
---
"""
        )

        manager = SkillManager(mock_config)

        with pytest.raises(SkillNotFoundError) as exc_info:
            manager.get_skill_content("unknown")

        assert exc_info.value.skill_name == "unknown"
        assert "known" in exc_info.value.available_skills


class TestSkillsPromptSection:
    """Tests for generating the available skills prompt section."""

    def test_generates_empty_string_when_no_skills(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        manager = SkillManager(mock_config)
        assert manager.get_skills_prompt_section() == ""

    def test_generates_xml_section_with_skills(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "test-skill"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: test-skill
description: A skill for testing prompt generation.
---
"""
        )

        manager = SkillManager(mock_config)
        section = manager.get_skills_prompt_section()

        assert "<available_skills>" in section
        assert "</available_skills>" in section
        assert "name: test-skill" in section
        assert "description: A skill for testing prompt generation." in section

    def test_has_skills_returns_false_when_empty(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        manager = SkillManager(mock_config)
        assert manager.has_skills() is False

    def test_has_skills_returns_true_when_skills_exist(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "exists"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: exists
description: Skill exists.
---
"""
        )

        manager = SkillManager(mock_config)
        assert manager.has_skills() is True


class TestSkillInfo:
    """Tests for the SkillInfo model."""

    def test_gets_skill_info(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        skill_path = skills_dir / "info-test"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            """---
name: info-test
description: Testing skill info retrieval.
---
"""
        )

        manager = SkillManager(mock_config)
        info = manager.get_skill_info("info-test")

        assert info is not None
        assert info.name == "info-test"
        assert info.description == "Testing skill info retrieval."
        assert info.path == skill_path / "SKILL.md"

    def test_returns_none_for_unknown_skill(
        self, mock_config: MagicMock, skills_dir: Path
    ) -> None:
        manager = SkillManager(mock_config)
        assert manager.get_skill_info("nonexistent") is None
