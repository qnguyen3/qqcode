from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolResult,
    BaseToolState,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.truncate import DEFAULT_MAX_BYTES, format_notice, truncate_head


class LsArgs(BaseModel):
    path: str = Field(default=".", description="Directory to list.")
    limit: int | None = Field(
        default=None, description="Maximum number of entries to return."
    )


class LsResult(BaseToolResult):
    path: str
    entries: str
    entry_count: int
    notice: str | None = None


class LsConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS
    default_limit: int = Field(
        default=500, description="Default maximum number of entries."
    )
    timeout: float | None = 120.0


class Ls(BaseTool[LsArgs, LsResult, LsConfig, BaseToolState]):
    description: ClassVar[str] = (
        "List directory contents alphabetically. Directories get a trailing '/', "
        "dotfiles are included."
    )
    scriptable: ClassVar[bool] = True

    async def run(self, args: LsArgs) -> LsResult:
        base = self._resolve_base_path(args.path)
        limit = args.limit if args.limit is not None else self.config.default_limit
        if limit <= 0:
            raise ToolError("limit must be a positive number")

        try:
            children = sorted(base.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ToolError(f"Error listing {base}: {exc}") from exc

        entries = [
            f"{child.name}/" if child.is_dir() else child.name for child in children
        ]
        truncated = truncate_head(
            "\n".join(entries), max_lines=limit, max_bytes=DEFAULT_MAX_BYTES
        )

        return LsResult(
            path=str(base),
            entries=truncated.content,
            entry_count=len(truncated.content.splitlines()) if truncated.content else 0,
            truncated=truncated.truncated,
            notice=format_notice(truncated) or None,
        )

    def _resolve_base_path(self, raw_path: str) -> Path:
        base = Path(raw_path).expanduser()
        if not base.is_absolute():
            base = self.config.effective_workdir / base
        base = base.resolve()
        if not base.exists():
            raise ToolError(f"Path does not exist: {raw_path}")
        if not base.is_dir():
            raise ToolError(f"Path is not a directory: {raw_path}")
        return base
