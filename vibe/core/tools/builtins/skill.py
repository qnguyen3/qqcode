from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from vibe.core.skills.manager import SkillManager, SkillNotFoundError
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData

if TYPE_CHECKING:
    from vibe.core.types import ToolCallEvent, ToolResultEvent


class SkillArgs(BaseModel):
    """Arguments for the skill tool."""

    name: str = Field(description="The name of the skill to activate.")


class SkillResult(BaseModel):
    """Result from activating a skill."""

    skill_name: str
    content: str
    skill_path: str
    success: bool = True
    error: str | None = None


class SkillConfig(BaseToolConfig):
    """Configuration for the skill tool."""

    permission: ToolPermission = ToolPermission.ASK


class SkillState(BaseToolState):
    """State for the skill tool.

    Holds a reference to the SkillManager for accessing skill content.
    """

    skill_manager: SkillManager | None = Field(default=None, exclude=True)


class Skill(
    BaseTool[SkillArgs, SkillResult, SkillConfig, SkillState],
    ToolUIData[SkillArgs, SkillResult],
):
    """Tool to activate a skill by name.

    When invoked, returns the full content of the skill's SKILL.md file,
    which contains instructions and context for the LLM to follow.
    """

    description: ClassVar[str] = (
        "Activate a skill by name to load domain-specific expertise. "
        "Returns the full skill content with instructions and context."
    )

    async def run(self, args: SkillArgs) -> SkillResult:
        """Load and return the content of the specified skill.

        Args:
            args: The skill arguments containing the skill name.

        Returns:
            SkillResult with the skill content or error information.

        Raises:
            ToolError: If the skill manager is not available or skill not found.
        """
        if self.state.skill_manager is None:
            raise ToolError(
                "Skill manager not initialized. Skills feature may not be enabled."
            )

        try:
            content = self.state.skill_manager.get_skill_content(args.name)
            skill_info = self.state.skill_manager.get_skill_info(args.name)
            skill_path = str(skill_info.path) if skill_info else "unknown"

            return SkillResult(
                skill_name=args.name,
                content=content,
                skill_path=skill_path,
                success=True,
            )
        except SkillNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError(f"Error reading skill '{args.name}': {exc}") from exc

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        """Generate display information for skill tool calls."""
        if not isinstance(event.args, SkillArgs):
            return ToolCallDisplay(summary="skill")

        return ToolCallDisplay(
            summary=f"Activating skill: {event.args.name}",
            details={"skill_name": event.args.name},
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        """Generate display information for skill tool results."""
        if event.error:
            return ToolResultDisplay(success=False, message=event.error)

        if event.skip_reason:
            return ToolResultDisplay(success=False, message=event.skip_reason)

        result = event.result

        # Use duck typing to check for SkillResult-like objects
        # This handles cases where the result might be deserialized or recreated
        if result is None or not hasattr(result, "skill_name"):
            return ToolResultDisplay(success=False, message="Invalid result type")

        # Access attributes safely
        skill_name = getattr(result, "skill_name", "unknown")
        content = getattr(result, "content", "")
        skill_path = getattr(result, "skill_path", "")
        success = getattr(result, "success", True)
        error = getattr(result, "error", None)

        if not success:
            return ToolResultDisplay(
                success=False,
                message=error or "Unknown error",
            )

        # Count content lines for summary
        content_lines = len(content.splitlines()) if content else 0
        message = f"Loaded skill '{skill_name}' ({content_lines} lines)"

        return ToolResultDisplay(
            success=True,
            message=message,
            details={
                "skill_name": skill_name,
                "skill_path": skill_path,
                "content_length": len(content) if content else 0,
                "content": content,
            },
        )

    @classmethod
    def get_status_text(cls) -> str:
        """Return status text for UI display."""
        return "Loading skill"

    @classmethod
    def create_with_skill_manager(
        cls, config: SkillConfig, skill_manager: SkillManager
    ) -> Skill:
        """Create a Skill tool instance with an injected SkillManager.

        Args:
            config: The tool configuration.
            skill_manager: The SkillManager instance for accessing skills.

        Returns:
            A configured Skill tool instance.
        """
        state = SkillState(skill_manager=skill_manager)
        return cls(config=config, state=state)
