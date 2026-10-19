from __future__ import annotations

import asyncio
from fnmatch import fnmatch
from pathlib import Path
import shutil
from typing import ClassVar

from pydantic import BaseModel, Field

from vibe.core.autocompletion.file_indexer.ignore_rules import IgnoreRules
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolResult,
    BaseToolState,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.truncate import DEFAULT_MAX_BYTES, format_notice, truncate_head


class FindArgs(BaseModel):
    pattern: str = Field(description="Glob pattern matched against each full path.")
    path: str = Field(default=".", description="Directory to search in.")
    limit: int | None = Field(
        default=None, description="Maximum number of matches to return."
    )


class FindResult(BaseToolResult):
    path: str
    pattern: str
    matches: str
    match_count: int
    notice: str | None = None


class FindConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS
    default_limit: int = Field(
        default=1000, description="Default maximum number of matches."
    )
    timeout: float | None = 120.0


class Find(BaseTool[FindArgs, FindResult, FindConfig, BaseToolState]):
    description: ClassVar[str] = (
        "Find files and directories by glob pattern, honouring .gitignore. "
        "Uses fd when available and falls back to a built-in walk."
    )
    scriptable: ClassVar[bool] = True

    async def run(self, args: FindArgs) -> FindResult:
        base = self._resolve_base_path(args.path)
        limit = args.limit if args.limit is not None else self.config.default_limit
        if limit <= 0:
            raise ToolError("limit must be a positive number")

        relative_matches = await self._collect_matches(base, args.pattern)
        truncated = truncate_head(
            "\n".join(relative_matches), max_lines=limit, max_bytes=DEFAULT_MAX_BYTES
        )

        return FindResult(
            path=str(base),
            pattern=args.pattern,
            matches=truncated.content,
            match_count=len(truncated.content.splitlines()) if truncated.content else 0,
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

    async def _collect_matches(self, base: Path, pattern: str) -> list[str]:
        if fd_path := shutil.which("fd"):
            matches = await self._collect_with_fd(fd_path, base, pattern)
            if matches:
                return matches
        return self._collect_with_walk(base, pattern)

    async def _collect_with_fd(
        self, fd_path: str, base: Path, pattern: str
    ) -> list[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                fd_path,
                "--glob",
                "--full-path",
                "--color",
                "never",
                pattern,
                ".",
                cwd=str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await proc.communicate()
        except OSError as exc:
            raise ToolError(f"Error running fd: {exc}") from exc

        matches: list[str] = []
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            candidate = (base / line).resolve()
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                relative = Path(line)
            suffix = "/" if candidate.is_dir() else ""
            matches.append(f"{relative}{suffix}")
        matches.sort()
        return matches

    def _collect_with_walk(self, base: Path, pattern: str) -> list[str]:
        ignore = IgnoreRules()
        ignore.ensure_for_root(base)

        matches: list[str] = []
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue

            for child in children:
                try:
                    relative = child.relative_to(base)
                except ValueError:
                    continue

                is_dir = child.is_dir()
                if ignore.should_ignore(str(relative), child.name, is_dir):
                    continue

                if fnmatch(str(relative), pattern):
                    suffix = "/" if is_dir else ""
                    matches.append(f"{relative}{suffix}")

                if is_dir:
                    stack.append(child)

        matches.sort()
        return matches
