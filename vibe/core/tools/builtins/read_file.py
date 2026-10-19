from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NamedTuple, final

import aiofiles
from pydantic import BaseModel, Field

from vibe.core.images import (
    MAX_IMAGE_BYTES,
    ImageError,
    describe_image,
    is_image_path,
    pillow_available,
)
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolResult,
    BaseToolState,
    ToolError,
    ToolPermission,
    check_path_patterns,
    resolve_tool_path,
)
from vibe.core.tools.truncate import DEFAULT_MAX_LINES, format_notice, truncate_head
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ImagePart

if TYPE_CHECKING:
    from vibe.core.types import ToolCallEvent, ToolResultEvent


class _ReadResult(NamedTuple):
    lines: list[str]
    bytes_read: int
    was_truncated: bool
    total_lines: int | None = None


class ReadFileArgs(BaseModel):
    path: str
    offset: int = Field(
        default=0,
        description="Line number to start reading from (0-indexed, inclusive).",
    )
    limit: int | None = Field(
        default=None, description="Maximum number of lines to read."
    )


class ReadFileResult(BaseToolResult):
    path: str
    content: str
    lines_read: int
    notice: str | None = None
    image: ImagePart | None = None

    @property
    def was_truncated(self) -> bool:
        return self.truncated


class ReadFileToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS

    max_read_bytes: int = Field(
        default=64_000, description="Maximum total bytes to read from a file in one go."
    )
    max_state_history: int = Field(
        default=10, description="Number of recently read files to remember in state."
    )
    timeout: float | None = Field(
        default=120.0, description="Agent-level timeout for the whole tool invocation."
    )


class ReadFileState(BaseToolState):
    recently_read_files: list[str] = Field(default_factory=list)


class ReadFile(
    BaseTool[ReadFileArgs, ReadFileResult, ReadFileToolConfig, ReadFileState],
    ToolUIData[ReadFileArgs, ReadFileResult],
):
    description: ClassVar[str] = (
        "Read a UTF-8 file, returning content from a specific line range. "
        "Reading is capped by a byte limit for safety."
    )
    scriptable: ClassVar[bool] = True

    @final
    async def run(self, args: ReadFileArgs) -> ReadFileResult:
        file_path = self._prepare_and_validate_path(args)

        if is_image_path(file_path):
            result = self._read_image(file_path)
            self._update_state_history(file_path)
            return result

        read_result = await self._read_file(args, file_path)

        self._update_state_history(file_path)

        text = "".join(read_result.lines)
        max_lines = args.limit or DEFAULT_MAX_LINES
        truncated = truncate_head(
            text, max_lines=max_lines, max_bytes=self.config.max_read_bytes
        )
        if truncated.truncated and read_result.total_lines is not None:
            truncated = replace(truncated, total_lines=read_result.total_lines)

        notice = format_notice(truncated)
        if truncated.truncated:
            next_offset = args.offset + (truncated.last_line or 0)
            notice = f"{notice} Use offset={next_offset} to continue reading."

        return ReadFileResult(
            path=str(file_path),
            content=truncated.content,
            lines_read=len(truncated.content.splitlines()),
            truncated=truncated.truncated,
            notice=notice or None,
        )

    def check_allowlist_denylist(self, args: ReadFileArgs) -> ToolPermission | None:
        import fnmatch

        file_path = Path(args.path).expanduser()
        if not file_path.is_absolute():
            file_path = self.config.effective_workdir / file_path
        file_str = str(file_path)

        for pattern in self.config.denylist:
            if fnmatch.fnmatch(file_str, pattern):
                return ToolPermission.NEVER

        for pattern in self.config.allowlist:
            if fnmatch.fnmatch(file_str, pattern):
                return ToolPermission.ALWAYS

        return None

    def check_path_restrictions(self, args: ReadFileArgs) -> ToolPermission | None:
        return check_path_patterns(
            resolve_tool_path(args.path, self.config.effective_workdir),
            allowlist=self.config.path_allowlist,
            denylist=self.config.path_denylist,
        )

    def _prepare_and_validate_path(self, args: ReadFileArgs) -> Path:
        self._validate_inputs(args)

        file_path = Path(args.path).expanduser()
        if not file_path.is_absolute():
            file_path = self.config.effective_workdir / file_path

        self._validate_path(file_path)
        return file_path

    async def _read_file(self, args: ReadFileArgs, file_path: Path) -> _ReadResult:
        try:
            stored: list[str] = []
            stored_bytes = 0
            bytes_read = 0
            total_lines = 0

            async with aiofiles.open(file_path, encoding="utf-8", errors="ignore") as f:
                line_index = 0
                async for line in f:
                    if line_index < args.offset:
                        line_index += 1
                        continue

                    if args.limit is not None and total_lines >= args.limit:
                        break

                    line_bytes = len(line.encode("utf-8"))
                    total_lines += 1
                    bytes_read += line_bytes

                    # Keep enough lines to render the byte-truncated head.
                    if stored_bytes <= self.config.max_read_bytes:
                        stored.append(line)
                        stored_bytes += line_bytes

                    line_index += 1

            return _ReadResult(
                lines=stored,
                bytes_read=bytes_read,
                was_truncated=total_lines > len(stored),
                total_lines=total_lines,
            )

        except OSError as exc:
            raise ToolError(f"Error reading {file_path}: {exc}") from exc

    def _validate_inputs(self, args: ReadFileArgs) -> None:
        if not args.path.strip():
            raise ToolError("Path cannot be empty")
        if args.offset < 0:
            raise ToolError("Offset cannot be negative")
        if args.limit is not None and args.limit <= 0:
            raise ToolError("Limit, if provided, must be a positive number")

    def _validate_path(self, file_path: Path) -> None:
        try:
            resolved_path = file_path.resolve()
        except ValueError:
            raise ToolError(
                f"Security error: Cannot read path '{file_path}' outside of the project directory '{self.config.effective_workdir}'."
            )
        except FileNotFoundError:
            raise ToolError(f"File not found at: {file_path}")

        if not resolved_path.exists():
            raise ToolError(f"File not found at: {file_path}")
        if resolved_path.is_dir():
            raise ToolError(f"Path is a directory, not a file: {file_path}")

    def _read_image(self, file_path: Path) -> ReadFileResult:
        try:
            image = describe_image(file_path)
        except ImageError as exc:
            raise ToolError(str(exc)) from exc

        if (
            image.size_bytes is not None
            and image.size_bytes > MAX_IMAGE_BYTES
            and not pillow_available()
        ):
            raise ToolError(
                f"Image {file_path} is {image.size_bytes} bytes, over the "
                f"{MAX_IMAGE_BYTES} byte limit. Install Pillow to resize it "
                "automatically."
            )

        dimensions = (
            f", {image.width}x{image.height}" if image.width and image.height else ""
        )
        size = f"{image.size_bytes} bytes" if image.size_bytes else "unknown size"
        return ReadFileResult(
            path=str(file_path),
            content=(
                f"Image {file_path.name} ({image.media_type}, {size}{dimensions}). "
                "The image is attached when the active model supports vision."
            ),
            lines_read=0,
            image=image,
        )

    def _update_state_history(self, file_path: Path) -> None:
        self.state.recently_read_files.append(str(file_path.resolve()))
        if len(self.state.recently_read_files) > self.config.max_state_history:
            self.state.recently_read_files.pop(0)

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        if not isinstance(event.args, ReadFileArgs):
            return ToolCallDisplay(summary="read_file")

        summary = f"read_file: {event.args.path}"
        if event.args.offset > 0 or event.args.limit is not None:
            parts = []
            if event.args.offset > 0:
                parts.append(f"from line {event.args.offset}")
            if event.args.limit is not None:
                parts.append(f"limit {event.args.limit} lines")
            summary += f" ({', '.join(parts)})"

        return ToolCallDisplay(
            summary=summary,
            details={
                "path": event.args.path,
                "offset": event.args.offset,
                "limit": event.args.limit,
            },
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if not isinstance(event.result, ReadFileResult):
            return ToolResultDisplay(
                success=False, message=event.error or event.skip_reason or "No result"
            )

        path_obj = Path(event.result.path)
        if event.result.image is not None:
            image = event.result.image
            size = f"{image.size_bytes} bytes" if image.size_bytes else "image"
            return ToolResultDisplay(
                success=True,
                message=f"Read {image.media_type} image from {path_obj.name} ({size})",
                details={
                    "path": str(event.result.path),
                    "media_type": image.media_type,
                    "size_bytes": image.size_bytes,
                    "width": image.width,
                    "height": image.height,
                    "content": event.result.content,
                    "file_extension": path_obj.suffix.lstrip(".")
                    if path_obj.suffix
                    else "image",
                },
            )

        message = f"Read {event.result.lines_read} line{'' if event.result.lines_read <= 1 else 's'} from {path_obj.name}"
        warnings: list[str] = []
        if event.result.was_truncated:
            message += " (truncated)"
            warnings.append("File was truncated due to size limit")
        if event.result.notice:
            warnings.append(event.result.notice)

        return ToolResultDisplay(
            success=True,
            message=message,
            warnings=warnings,
            details={
                "path": str(event.result.path),
                "lines_read": event.result.lines_read,
                "was_truncated": event.result.was_truncated,
                "notice": event.result.notice,
                "full_output_path": event.result.full_output_path,
                "content": event.result.content,
                "file_extension": path_obj.suffix.lstrip(".")
                if path_obj.suffix
                else "text",
            },
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Reading file"
