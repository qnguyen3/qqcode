from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolResult,
    BaseToolState,
    ToolError,
)
from vibe.core.tools.search import TOOL_SEARCH_TOOL_NAME, search_tools
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolCallEvent, ToolResultEvent

if TYPE_CHECKING:
    from vibe.core.tools.manager import ToolManager


class ToolSearchArgs(BaseModel):
    query: str = Field(
        description="Keywords describing the capability you need, e.g. 'create issue'."
    )
    limit: int = Field(
        default=8, ge=1, le=25, description="Maximum number of tools to return."
    )


class ToolSearchMatch(BaseModel):
    name: str
    description: str = ""
    score: float = 0.0


class ToolSearchResult(BaseToolResult):
    query: str
    matches: list[ToolSearchMatch] = Field(default_factory=list)
    exposed: list[str] = Field(default_factory=list)

    def format_for_model(self) -> str:
        if not self.matches:
            return f"No deferred tools matched '{self.query}'."
        lines = [f"Exposed {len(self.exposed)} tool(s) for the next request:"]
        for match in self.matches:
            detail = f" - {match.description}" if match.description else ""
            lines.append(f"- {match.name}{detail}")
        return "\n".join(lines)


class ToolSearch(
    BaseTool[ToolSearchArgs, ToolSearchResult, BaseToolConfig, BaseToolState],
    ToolUIData[ToolSearchArgs, ToolSearchResult],
):
    """Search tools that are hidden from the default tool list."""

    description: ClassVar[str] = (
        "Search for tools that are not currently in the tool list (deferred "
        "exposure) by name and description. Matching tools are exposed and "
        "become callable on the next request."
    )

    _manager: ToolManager | None = None

    @classmethod
    def create_with_manager(
        cls, config: BaseToolConfig, manager: ToolManager
    ) -> ToolSearch:
        instance = cls(config=config, state=BaseToolState())
        instance._manager = manager
        return instance

    @classmethod
    def get_name(cls) -> str:
        return TOOL_SEARCH_TOOL_NAME

    async def run(self, args: ToolSearchArgs) -> ToolSearchResult:
        if self._manager is None:
            raise ToolError("Tool search is not connected to a tool manager")

        pool = self._manager.deferred_pool()
        matches = search_tools(args.query, pool, limit=args.limit)
        exposed = self._manager.expose([match.name for match in matches])
        return ToolSearchResult(
            query=args.query,
            matches=[
                ToolSearchMatch(
                    name=match.name, description=match.description, score=match.score
                )
                for match in matches
            ],
            exposed=exposed,
        )

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        query = getattr(event.args, "query", "")
        return ToolCallDisplay(summary=TOOL_SEARCH_TOOL_NAME, details={"query": query})

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if not isinstance(event.result, ToolSearchResult):
            return ToolResultDisplay(
                success=False, message=event.error or event.skip_reason or "No result"
            )
        return ToolResultDisplay(
            success=True,
            message=f"Exposed {len(event.result.exposed)} tool(s)",
            details={"matches": [match.name for match in event.result.matches]},
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Searching tools"
