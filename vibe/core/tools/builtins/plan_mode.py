from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    ToolPermission,
)

# ============================================================================
# EnterPlanMode Tool
# ============================================================================


class EnterPlanModeArgs(BaseModel):
    """Arguments for entering plan mode."""

    pass


class EnterPlanModeResult(BaseModel):
    """Result of entering plan mode."""

    success: bool
    message: str
    mode_change: Literal["plan"] = "plan"


class EnterPlanModeConfig(BaseToolConfig):
    """Configuration for enter_plan_mode tool."""

    permission: ToolPermission = ToolPermission.ALWAYS


class EnterPlanMode(
    BaseTool[EnterPlanModeArgs, EnterPlanModeResult, EnterPlanModeConfig, BaseToolState]
):
    """Tool to enter plan mode for analyzing and creating implementation plans."""

    description: ClassVar[str] = (
        "Enter plan mode to analyze the codebase and create implementation plans. "
        "In plan mode, only read-only operations (read_file, grep, todo) are allowed. "
        "Use this when you need to plan before making changes."
    )

    async def run(self, args: EnterPlanModeArgs) -> EnterPlanModeResult:
        return EnterPlanModeResult(
            success=True,
            message="Entered plan mode. Only read-only operations are now allowed. "
            "Use exit_plan_mode when your plan is complete.",
        )


# ============================================================================
# ExitPlanMode Tool
# ============================================================================


class ExitPlanModeArgs(BaseModel):
    """Arguments for exiting plan mode."""

    pass


class ExitPlanModeResult(BaseModel):
    """Result of exiting plan mode."""

    success: bool
    message: str
    exit_plan_mode: Literal[True] = True


class ExitPlanModeConfig(BaseToolConfig):
    """Configuration for exit_plan_mode tool."""

    permission: ToolPermission = ToolPermission.ALWAYS


class ExitPlanMode(
    BaseTool[ExitPlanModeArgs, ExitPlanModeResult, ExitPlanModeConfig, BaseToolState]
):
    """Tool to exit plan mode after completing a plan."""

    description: ClassVar[str] = (
        "Exit plan mode after you have completed your implementation plan. "
        "The user will then choose how to proceed with execution "
        "(auto-approve, interactive, or revise the plan)."
    )

    async def run(self, args: ExitPlanModeArgs) -> ExitPlanModeResult:
        return ExitPlanModeResult(
            success=True,
            message="Plan complete. Awaiting user approval to proceed with execution.",
        )
