from __future__ import annotations

from logging import getLogger
from pathlib import Path
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vibe.core.config import get_vibe_home
from vibe.core.packages import package_resource_dirs

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

logger = getLogger("vibe")

# Regex to match YAML frontmatter block
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Regex to extract key-value pairs from frontmatter
KEY_VALUE_PATTERN = re.compile(r"^([\w-]+):\s*(.+)$", re.MULTILINE)

DISABLE_MODEL_INVOCATION_VALUES = frozenset({"true", "yes", "1"})
ALLOWED_TOOLS_SEPARATOR_PATTERN = re.compile(r"[,\s]+")


class SkillInfo(BaseModel):
    """Information about a discovered skill.

    Attributes:
        name: The unique name of the skill.
        description: A description of when and how to use the skill.
        path: The path to the SKILL.md file.
        disable_model_invocation: Hide the skill from the model-facing listing.
        allowed_tools: Tools the skill is allowed to use, if declared.
    """

    name: str
    description: str
    path: Path
    disable_model_invocation: bool = False
    allowed_tools: list[str] | None = None


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
        1. Project-local: ./.qqcode/skills/ (cwd up to root)
        2. Project-local: ./.agents/skills/ (cwd up to root)
        3. Installed resource packages
        4. Global: ~/.qqcode/skills/
        5. Global: ~/.agents/skills/
        """
        paths: list[Path] = []

        cwd = config.effective_workdir
        for root in (".qqcode", ".agents"):
            for directory in (cwd, *cwd.parents):
                skills_dir = directory / root / "skills"
                if skills_dir.is_dir():
                    paths.append(skills_dir)
                    break

        paths.extend(package_resource_dirs(config.packages, "skills"))

        global_paths = (get_vibe_home() / "skills", Path.home() / ".agents" / "skills")
        paths.extend(path for path in global_paths if path.is_dir())

        unique: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

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

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Remove the YAML frontmatter block from SKILL.md content."""
        return FRONTMATTER_PATTERN.sub("", content, count=1)

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
                        logger.warning(
                            "Duplicate skill '%s' from %s ignored; "
                            "a higher-priority definition already loaded",
                            name,
                            skill_dir,
                        )
                        continue

                    seen_names.add(name)
                    allowed_tools_raw = frontmatter.get("allowed-tools", "").strip()
                    allowed_tools = [
                        tool
                        for tool in ALLOWED_TOOLS_SEPARATOR_PATTERN.split(
                            allowed_tools_raw
                        )
                        if tool
                    ]
                    self._skills[name] = SkillInfo(
                        name=name,
                        description=description,
                        path=skill_file,
                        disable_model_invocation=frontmatter.get(
                            "disable-model-invocation", ""
                        )
                        .strip()
                        .lower()
                        in DISABLE_MODEL_INVOCATION_VALUES,
                        allowed_tools=allowed_tools or None,
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

        Skills declaring ``disable-model-invocation`` are hidden from the
        listing; they remain callable through ``/skill:<name>``.

        Returns:
            An XML string listing all available skills, or empty string if none.
        """
        visible_skills = [
            skill
            for skill in self._skills.values()
            if not skill.disable_model_invocation
        ]
        if not visible_skills:
            return ""

        lines = ["<available_skills>"]
        for skill in visible_skills:
            lines.extend([
                "  <skill>",
                f"    <name>{skill.name}</name>",
                f"    <description>{skill.description}</description>",
                f"    <location>{skill.path.resolve()}</location>",
                "  </skill>",
            ])
        lines.append("</available_skills>")
        lines.append("")
        lines.append(
            "When a task matches a skill's description, read the file at "
            "location when the task matches to load its full instructions. "
            "Relative paths inside a skill resolve against its directory."
        )

        return "\n".join(lines)

    def render_skill_invocation(self, name: str, raw_args: str = "") -> str:
        """Render a skill as a user message for explicit ``/skill:<name>`` calls.

        The frontmatter is stripped and the body is wrapped in a
        ``<skill name="..." location="...">`` tag, with ``raw_args`` appended.

        Raises:
            SkillNotFoundError: If the skill is not found.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(name, list(self._skills.keys()))

        body = self.strip_frontmatter(skill.path.read_text(encoding="utf-8")).strip()
        args = raw_args.strip()
        content = f"{body}\n\n{args}" if args else body
        return (
            f'<skill name="{skill.name}" location="{skill.path.resolve()}">\n'
            f"{content}\n"
            "</skill>"
        )

    def has_skills(self) -> bool:
        """Check if any skills are available."""
        return bool(self._skills)
