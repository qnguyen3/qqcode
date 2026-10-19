from __future__ import annotations

from collections.abc import Callable
from math import log
import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from vibe.core.tools.base import BaseTool, BaseToolResult, BaseToolState, ToolError
from vibe.core.tools.mcp import MCPToolConfig
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay
from vibe.core.types import ToolCallEvent, ToolResultEvent

MCP_SEARCH_TOOL_NAME = "mcp_search"

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
BM25_K1 = 1.5
BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def bm25_scores(query: str, documents: dict[str, str]) -> dict[str, float]:
    """Rank ``documents`` (keyed by tool name) against ``query`` with BM25."""
    if not documents:
        return {}
    tokenized = {key: _tokenize(text) for key, text in documents.items()}
    doc_count = len(tokenized)
    avg_len = sum(len(tokens) for tokens in tokenized.values()) / doc_count or 1.0

    query_terms = list(dict.fromkeys(_tokenize(query)))
    if not query_terms:
        return {key: 0.0 for key in documents}

    scores: dict[str, float] = {}
    for key, tokens in tokenized.items():
        length = len(tokens) or 1
        score = 0.0
        for term in query_terms:
            term_freq = tokens.count(term)
            if term_freq == 0:
                continue
            doc_freq = sum(1 for other in tokenized.values() if term in other)
            idf = log(1 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))
            denominator = term_freq + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len)
            score += idf * term_freq * (BM25_K1 + 1) / denominator
        scores[key] = score
    return scores


class MCPSearchMatch(BaseModel):
    name: str
    server: str
    description: str | None = None


class MCPSearchResult(BaseToolResult):
    query: str
    matches: list[MCPSearchMatch] = Field(default_factory=list)
    activated: list[str] = Field(default_factory=list)

    def format_for_model(self) -> str:
        if not self.matches:
            return f"No deferred MCP tools matched '{self.query}'."
        lines = [f"Activated {len(self.activated)} MCP tool(s) for the next request:"]
        for match in self.matches:
            detail = f" - {match.description}" if match.description else ""
            lines.append(f"- {match.name} [server: {match.server}]{detail}")
        return "\n".join(lines)


class _MCPSearchArgs(BaseModel):
    query: str = Field(
        description="Keywords describing the capability you need, e.g. 'create issue'."
    )


class DeferredToolRegistry:
    """Tracks deferred MCP tools and which ones were activated by a search."""

    def __init__(self) -> None:
        self._entries: dict[
            str, tuple[str, str | None, type[BaseTool[Any, Any, Any, Any]]]
        ] = {}
        self._active: set[str] = set()

    def set_tools(
        self,
        tools: dict[str, tuple[str, str | None, type[BaseTool[Any, Any, Any, Any]]]],
    ) -> None:
        self._entries = dict(tools)
        self._active = {name for name in self._active if name in tools}

    def clear(self) -> None:
        self._entries.clear()
        self._active.clear()

    def reset(self) -> None:
        """Drop all activations while keeping the deferred tool pool."""
        self._active.clear()

    def is_empty(self) -> bool:
        return not self._entries

    def search(self, query: str, *, top_k: int = 5) -> list[str]:
        documents = {
            name: " ".join(filter(None, (name, description)))
            for name, (_, description, _) in self._entries.items()
        }
        scores = bm25_scores(query, documents)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [name for name, score in ranked if score > 0][:top_k]

    def activate(self, names: list[str]) -> list[str]:
        activated = [name for name in names if name in self._entries]
        self._active.update(activated)
        return activated

    def active(self) -> set[str]:
        return {name for name in self._active if name in self._entries}

    def entry(
        self, name: str
    ) -> tuple[str, str | None, type[BaseTool[Any, Any, Any, Any]]] | None:
        return self._entries.get(name)


def create_mcp_search_tool_class(
    registry: DeferredToolRegistry,
    *,
    activate: Callable[[list[str]], list[str]] | None = None,
) -> type[BaseTool[_MCPSearchArgs, MCPSearchResult, MCPToolConfig, BaseToolState]]:
    """Create the ``mcp_search`` tool exposing deferred MCP tools on demand."""

    class MCPSearchTool(
        BaseTool[_MCPSearchArgs, MCPSearchResult, MCPToolConfig, BaseToolState]
    ):
        description: ClassVar[str] = (
            "Search MCP tools that are hidden by default (deferred exposure) by "
            "name and description. Matching tools are activated and become "
            "callable on the next request."
        )

        @classmethod
        def get_name(cls) -> str:
            return MCP_SEARCH_TOOL_NAME

        async def run(self, args: _MCPSearchArgs) -> MCPSearchResult:
            try:
                names = registry.search(args.query)
                activated = (
                    activate(names)
                    if activate is not None
                    else registry.activate(names)
                )
            except Exception as exc:
                raise ToolError(f"MCP tool search failed: {exc}") from exc

            matches = [
                MCPSearchMatch(name=name, server=entry[0], description=entry[1])
                for name in names
                if (entry := registry.entry(name)) is not None
            ]
            return MCPSearchResult(
                query=args.query, matches=matches, activated=activated
            )

        @classmethod
        def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
            query = getattr(event.args, "query", "")
            return ToolCallDisplay(
                summary=MCP_SEARCH_TOOL_NAME, details={"query": query}
            )

        @classmethod
        def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
            if not isinstance(event.result, MCPSearchResult):
                return ToolResultDisplay(
                    success=False,
                    message=event.error or event.skip_reason or "No result",
                )
            return ToolResultDisplay(
                success=True,
                message=f"Activated {len(event.result.activated)} MCP tool(s)",
            )

        @classmethod
        def get_status_text(cls) -> str:
            return "Searching MCP tools"

    MCPSearchTool.__name__ = "MCP_SEARCH"
    return MCPSearchTool
