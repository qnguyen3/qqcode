from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState


class SleepToolArgs(BaseModel):
    seconds: float = 0.0
    label: str = ""


class SleepToolResult(BaseModel):
    message: str = ""


class SleepToolState(BaseToolState):
    invocations: list[str] = Field(default_factory=list)


class SleepTool(
    BaseTool[SleepToolArgs, SleepToolResult, BaseToolConfig, SleepToolState]
):
    @classmethod
    def get_name(cls) -> str:
        return "sleep_tool"

    async def run(self, args: SleepToolArgs) -> SleepToolResult:
        self.state.invocations.append(args.label)
        await asyncio.sleep(args.seconds)
        return SleepToolResult(message=args.label or "done")
