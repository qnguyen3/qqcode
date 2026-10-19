from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, final, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState, ToolError
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData

if TYPE_CHECKING:
    from vibe.core.types import ToolCallEvent, ToolResultEvent


class TaskArgs(BaseModel):
    description: str = Field(
        description="3-5 word description of the delegated work, used for the UI."
    )
    prompt: str = Field(description="Full, self-contained task for the subagent.")
    agent: str | None = Field(
        default=None,
        description="Agent config name under ~/.qqcode/agents (default: general).",
    )
    max_turns: int | None = Field(
        default=15, description="Maximum number of subagent turns."
    )


class TaskResult(BaseModel):
    description: str
    output: str
    turns: int
    prompt_tokens: int
    completion_tokens: int
    success: bool
    error: str | None = None

    def format_for_model(self) -> str:
        if self.success:
            header = (
                f"Subagent '{self.description}' finished in {self.turns} turns "
                f"({self.prompt_tokens} prompt + {self.completion_tokens} completion tokens)."
            )
        else:
            header = (
                f"Subagent '{self.description}' failed after {self.turns} turns: "
                f"{self.error or 'unknown error'}"
            )
        output = self.output.strip() or "(no output)"
        return f"{header}\nOutput:\n{output}"


class TaskConfig(BaseToolConfig):
    """Configuration for the task tool; budgets live on the subagent config."""


@runtime_checkable
class TaskRunner(Protocol):
    """Structural type for the object that executes a delegated task."""

    async def run(self, args: TaskArgs) -> TaskResult: ...


class TaskState(BaseToolState):
    model_config = ConfigDict(
        arbitrary_types_allowed=True, validate_default=True, extra="forbid"
    )
    runner: TaskRunner | None = Field(default=None, exclude=True)


class Task(
    BaseTool[TaskArgs, TaskResult, TaskConfig, TaskState],
    ToolUIData[TaskArgs, TaskResult],
):
    description: ClassVar[str] = (
        "Delegate an independent, self-contained task to a subagent that runs "
        "with its own context, restricted tools and a turn/time budget."
    )

    def attach_runner(self, runner: TaskRunner) -> None:
        """Inject the runner used to execute delegated tasks."""
        self.state.runner = runner

    @final
    async def run(self, args: TaskArgs) -> TaskResult:
        runner = self.state.runner
        if runner is None:
            raise ToolError(
                "task tool is unavailable: no subagent runner is attached to this agent"
            )
        return await runner.run(args)

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        if not isinstance(event.args, TaskArgs):
            return ToolCallDisplay(summary="task")

        return ToolCallDisplay(
            summary=f"task: {event.args.description}",
            details={
                "description": event.args.description,
                "prompt": event.args.prompt,
                "agent": event.args.agent,
                "max_turns": event.args.max_turns,
            },
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if event.error:
            return ToolResultDisplay(success=False, message=event.error)

        result = event.result
        if result is None:
            return ToolResultDisplay(
                success=False, message=event.skip_reason or "No result"
            )

        # ToolManager discovers builtins dynamically, so the result instance
        # may not be this module's class; read it structurally.
        success = bool(getattr(result, "success", False))
        turns = int(getattr(result, "turns", 0))
        prompt_tokens = int(getattr(result, "prompt_tokens", 0))
        completion_tokens = int(getattr(result, "completion_tokens", 0))
        error = getattr(result, "error", None)
        details = {
            "description": str(getattr(result, "description", "")),
            "turns": turns,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "output": str(getattr(result, "output", "")),
        }
        if not success:
            return ToolResultDisplay(
                success=False, message=str(error or "subagent failed"), details=details
            )

        tokens = prompt_tokens + completion_tokens
        return ToolResultDisplay(
            success=True,
            message=f"Completed: {turns} turns, {tokens} tokens",
            details=details,
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Running subagent"
