from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibe.core.skills.manager import SkillManager
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.skill import (
    Skill,
    SkillArgs,
    SkillConfig,
    SkillResult,
    SkillState,
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


@pytest.fixture
def skill_manager(mock_config: MagicMock, skills_dir: Path) -> SkillManager:
    """Create a SkillManager with a test skill."""
    skill_path = skills_dir / "test-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(
        """---
name: test-skill
description: A test skill for unit testing.
---

# Test Skill

This is the test skill content.

## Instructions

Follow these instructions.
"""
    )
    return SkillManager(mock_config)


@pytest.fixture
def skill_tool(skill_manager: SkillManager) -> Skill:
    """Create a Skill tool with an injected SkillManager."""
    config = SkillConfig()
    return Skill.create_with_skill_manager(config, skill_manager)


class TestSkillTool:
    """Tests for the Skill tool."""

    @pytest.mark.asyncio
    async def test_loads_skill_content(self, skill_tool: Skill) -> None:
        args = SkillArgs(name="test-skill")
        result = await skill_tool.run(args)

        assert isinstance(result, SkillResult)
        assert result.skill_name == "test-skill"
        assert result.success is True
        assert "# Test Skill" in result.content
        assert "This is the test skill content." in result.content

    @pytest.mark.asyncio
    async def test_raises_error_for_unknown_skill(self, skill_tool: Skill) -> None:
        args = SkillArgs(name="nonexistent-skill")

        with pytest.raises(ToolError) as exc_info:
            await skill_tool.run(args)

        assert "nonexistent-skill" in str(exc_info.value)
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_raises_error_without_skill_manager(self) -> None:
        # Create skill tool without skill manager
        config = SkillConfig()
        state = SkillState(skill_manager=None)
        tool = Skill(config=config, state=state)

        args = SkillArgs(name="any-skill")

        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert "not initialized" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_returns_skill_path(
        self, skill_tool: Skill, skills_dir: Path
    ) -> None:
        args = SkillArgs(name="test-skill")
        result = await skill_tool.run(args)

        expected_path = skills_dir / "test-skill" / "SKILL.md"
        assert result.skill_path == str(expected_path)


class TestSkillToolCreation:
    """Tests for Skill tool creation methods."""

    def test_create_with_skill_manager(self, skill_manager: SkillManager) -> None:
        config = SkillConfig()
        tool = Skill.create_with_skill_manager(config, skill_manager)

        assert tool.state.skill_manager is skill_manager
        assert tool.config is config

    def test_from_config_without_skill_manager(self) -> None:
        config = SkillConfig()
        tool = Skill.from_config(config)

        assert tool.state.skill_manager is None


class TestSkillToolDisplay:
    """Tests for Skill tool display methods."""

    def test_get_call_display(self) -> None:
        from vibe.core.types import ToolCallEvent

        args = SkillArgs(name="frontend-design")
        event = ToolCallEvent(
            tool_name="skill",
            tool_class=Skill,
            args=args,
            tool_call_id="test-id",
        )

        display = Skill.get_call_display(event)

        assert "frontend-design" in display.summary
        assert display.details["skill_name"] == "frontend-design"

    def test_get_result_display_success(self) -> None:
        from vibe.core.types import ToolResultEvent

        result = SkillResult(
            skill_name="test-skill",
            content="# Test\n\nContent here.",
            skill_path="/path/to/SKILL.md",
            success=True,
        )
        event = ToolResultEvent(
            tool_name="skill",
            tool_class=Skill,
            result=result,
            tool_call_id="test-id",
        )

        display = Skill.get_result_display(event)

        assert display.success is True
        assert "test-skill" in display.message
        # Content has 3 lines: "# Test", "", "Content here."
        assert "3 lines" in display.message

    def test_get_result_display_error(self) -> None:
        from vibe.core.types import ToolResultEvent

        event = ToolResultEvent(
            tool_name="skill",
            tool_class=Skill,
            error="Skill not found",
            tool_call_id="test-id",
        )

        display = Skill.get_result_display(event)

        assert display.success is False
        assert "not found" in display.message.lower()

    def test_get_status_text(self) -> None:
        assert Skill.get_status_text() == "Loading skill"
