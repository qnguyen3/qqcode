from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    ToolPermission,
)

# ============================================================================
# SubmitPlan Tool
# ============================================================================


class SubmitPlanArgs(BaseModel):
    """Arguments for submitting a plan."""

    plan: str = Field(description="The implementation plan in Markdown format")


class SubmitPlanResult(BaseModel):
    """Result of submitting a plan."""

    success: bool
    message: str
    plan_submitted: Literal[True] = True
    approved: bool | None = None
    execution_mode: str | None = None


class SubmitPlanConfig(BaseToolConfig):
    """Configuration for submit_plan tool."""

    permission: ToolPermission = ToolPermission.ALWAYS


class SubmitPlan(
    BaseTool[SubmitPlanArgs, SubmitPlanResult, SubmitPlanConfig, BaseToolState]
):
    """Tool to submit an implementation plan for user approval."""

    description: ClassVar[str] = (
        "Submit your implementation plan for user approval. "
        "The plan should be in Markdown format with clear steps. "
        "The user will review and choose to approve or request revisions."
    )

    async def run(self, args: SubmitPlanArgs) -> SubmitPlanResult:
        # The actual approval happens via callback in the agent
        # This just returns a placeholder result that will be mutated
        return SubmitPlanResult(
            success=True,
            message="Plan submitted. Awaiting user approval.",
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
    """Tool to exit plan mode after user has approved the plan."""

    description: ClassVar[str] = (
        "Exit plan mode after your plan has been approved by the user. "
        "Call this to acknowledge approval and begin implementation."
    )

    async def run(self, args: ExitPlanModeArgs) -> ExitPlanModeResult:
        return ExitPlanModeResult(
            success=True,
            message="Exiting plan mode. Proceeding with implementation.",
        )
