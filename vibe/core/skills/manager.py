from __future__ import annotations

from logging import getLogger
from pathlib import Path
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vibe.core.config import get_vibe_home

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

logger = getLogger("vibe")

# Regex to match YAML frontmatter block
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Regex to extract key-value pairs from frontmatter
KEY_VALUE_PATTERN = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


class SkillInfo(BaseModel):
    """Information about a discovered skill.

    Attributes:
        name: The unique name of the skill.
        description: A description of when and how to use the skill.
        path: The path to the SKILL.md file.
    """

    name: str
    description: str
    path: Path


class SkillNotFoundError(Exception):
    """Raised when a requested skill is not found."""

    def __init__(self, skill_name: str, available_skills: list[str]) -> None:
        self.skill_name = skill_name
        self.available_skills = available_skills
        super().__init__(
            f"Skill '{skill_name}' not found. "
            f"Available skills: {', '.join(available_skills) if available_skills else 'none'}"
        )


class SkillManager:
    """Manages skill discovery and loading.

    Discovers available skills from the project-local and global skills directories.
    Each skill is defined by a SKILL.md file with YAML frontmatter containing
    name and description fields.
    """

    def __init__(self, config: VibeConfig) -> None:
        self._config = config
        self._search_paths = self._compute_search_paths(config)
        self._skills: dict[str, SkillInfo] = {}
        self._discover_skills()

    @staticmethod
    def _compute_search_paths(config: VibeConfig) -> list[Path]:
        """Compute the list of directories to search for skills.

        Priority order:
        1. Project-local: ./.qqcode/skills/
        2. Global: ~/.qqcode/skills/
        """
        paths: list[Path] = []

        # Project-local skills directory
        cwd = config.effective_workdir
        for directory in (cwd, *cwd.parents):
            skills_dir = directory / ".qqcode" / "skills"
            if skills_dir.is_dir():
                paths.append(skills_dir)
                break

        # Global skills directory
        global_skills = get_vibe_home() / "skills"
        if global_skills.is_dir():
            paths.append(global_skills)

        return paths

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, str]:
        """Parse YAML frontmatter from SKILL.md content.

        Args:
            content: The full content of a SKILL.md file.

        Returns:
            A dictionary of key-value pairs from the frontmatter.
        """
        match = FRONTMATTER_PATTERN.match(content)
        if not match:
            return {}

        frontmatter_text = match.group(1)
        result: dict[str, str] = {}

        for key_match in KEY_VALUE_PATTERN.finditer(frontmatter_text):
            key = key_match.group(1).strip()
            value = key_match.group(2).strip()
            # Remove surrounding quotes if present
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            result[key] = value

        return result

    def _discover_skills(self) -> None:
        """Discover all available skills from search paths."""
        seen_names: set[str] = set()

        for search_path in self._search_paths:
            if not search_path.is_dir():
                continue

            # Each subdirectory is a potential skill
            for skill_dir in search_path.iterdir():
                if not skill_dir.is_dir():
                    continue

                skill_file = skill_dir / "SKILL.md"
                if not skill_file.is_file():
                    logger.debug("Skipping %s: no SKILL.md found", skill_dir.name)
                    continue

                try:
                    content = skill_file.read_text(encoding="utf-8")
                    frontmatter = self._parse_frontmatter(content)

                    name = frontmatter.get("name", "").strip()
                    description = frontmatter.get("description", "").strip()

                    if not name:
                        logger.warning(
                            "Skipping skill in %s: missing 'name' in frontmatter",
                            skill_dir,
                        )
                        continue

                    if not description:
                        logger.warning(
                            "Skipping skill in %s: missing 'description' in frontmatter",
                            skill_dir,
                        )
                        continue

                    # Skip duplicates (first one wins - project takes priority)
                    if name in seen_names:
                        logger.debug(
                            "Skipping duplicate skill '%s' from %s", name, skill_dir
                        )
                        continue

                    seen_names.add(name)
                    self._skills[name] = SkillInfo(
                        name=name, description=description, path=skill_file
                    )
                    logger.info("Discovered skill: %s from %s", name, skill_file)

                except OSError as exc:
                    logger.warning("Error reading skill from %s: %s", skill_dir, exc)
                    continue

    def get_available_skills(self) -> list[SkillInfo]:
        """Return a list of all discovered skills."""
        return list(self._skills.values())

    def get_skill_content(self, name: str) -> str:
        """Get the full content of a skill's SKILL.md file.

        Args:
            name: The name of the skill to load.

        Returns:
            The full content of the SKILL.md file.

        Raises:
            SkillNotFoundError: If the skill is not found.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(name, list(self._skills.keys()))

        # Read content fresh (not cached) to support live editing
        return skill.path.read_text(encoding="utf-8")

    def get_skill_info(self, name: str) -> SkillInfo | None:
        """Get information about a specific skill.

        Args:
            name: The name of the skill.

        Returns:
            The SkillInfo if found, None otherwise.
        """
        return self._skills.get(name)

    def get_skills_prompt_section(self) -> str:
        """Generate the <available_skills> XML section for the system prompt.

        Returns:
            An XML string listing all available skills, or empty string if no skills.
        """
        if not self._skills:
            return ""

        lines = ["<available_skills>"]
        for skill in self._skills.values():
            lines.append(f"- name: {skill.name}")
            lines.append(f"  description: {skill.description}")
            lines.append("")
        lines.append("</available_skills>")

        return "\n".join(lines)

    def has_skills(self) -> bool:
        """Check if any skills are available."""
        return bool(self._skills)
