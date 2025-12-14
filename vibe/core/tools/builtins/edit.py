from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

import aiofiles
from pydantic import BaseModel, Field

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState, ToolError
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolCallEvent, ToolResultEvent


class EditArgs(BaseModel):
    file_path: str = Field(description="The path to the file to modify")
    old_string: str = Field(description="The exact text to find and replace")
    new_string: str = Field(description="The text to replace with")
    replace_all: bool = Field(
        default=False,
        description="If true, replace all occurrences. Otherwise, requires old_string to be unique.",
    )


class EditResult(BaseModel):
    file: str
    old_string: str
    new_string: str
    replacements: int


class EditConfig(BaseToolConfig):
    max_file_size: int = 100_000
    create_backup: bool = False


class EditState(BaseToolState):
    pass


class Edit(
    BaseTool[EditArgs, EditResult, EditConfig, EditState],
    ToolUIData[EditArgs, EditResult],
):
    description: ClassVar[str] = (
        "Make targeted edits to files by replacing exact text matches. "
        "Requires old_string to be unique in the file unless replace_all is true."
    )

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        if not isinstance(event.args, EditArgs):
            return ToolCallDisplay(summary="Invalid arguments")

        args = event.args
        return ToolCallDisplay(
            summary=f"Editing {args.file_path}",
            content=f"- {args.old_string}\n+ {args.new_string}",
            details={
                "path": args.file_path,
                "replace_all": args.replace_all,
            },
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if isinstance(event.result, EditResult):
            return ToolResultDisplay(
                success=True,
                message=f"Replaced {event.result.replacements} occurrence(s)",
                details={
                    "file": event.result.file,
                    "replacements": event.result.replacements,
                    "old_string": event.result.old_string,
                    "new_string": event.result.new_string,
                },
            )

        return ToolResultDisplay(success=True, message="Edit applied")

    @classmethod
    def get_status_text(cls) -> str:
        return "Editing file"

    @final
    async def run(self, args: EditArgs) -> EditResult:
        file_path = self._validate_and_resolve_path(args.file_path)
        original_content = await self._read_file(file_path)

        # Check if old_string exists
        if args.old_string not in original_content:
            raise ToolError(
                f"old_string not found in {file_path}.\n"
                f"The text to find was:\n{args.old_string!r}"
            )

        # Count occurrences
        occurrences = original_content.count(args.old_string)

        # If multiple occurrences and replace_all is False, error
        if occurrences > 1 and not args.replace_all:
            raise ToolError(
                f"old_string appears {occurrences} times in {file_path}. "
                f"Set replace_all=true to replace all occurrences, "
                f"or provide a more specific old_string that is unique."
            )

        # Perform replacement
        if args.replace_all:
            new_content = original_content.replace(args.old_string, args.new_string)
            replacements = occurrences
        else:
            new_content = original_content.replace(args.old_string, args.new_string, 1)
            replacements = 1

        # Write the file
        await self._write_file(file_path, new_content)

        return EditResult(
            file=str(file_path),
            old_string=args.old_string,
            new_string=args.new_string,
            replacements=replacements,
        )

    @final
    def _validate_and_resolve_path(self, file_path_str: str) -> Path:
        file_path_str = file_path_str.strip()

        if not file_path_str:
            raise ToolError("File path cannot be empty")

        project_root = self.config.effective_workdir
        file_path = Path(file_path_str).expanduser()
        if not file_path.is_absolute():
            file_path = project_root / file_path
        file_path = file_path.resolve()

        if not file_path.exists():
            raise ToolError(f"File does not exist: {file_path}")

        if not file_path.is_file():
            raise ToolError(f"Path is not a file: {file_path}")

        return file_path

    async def _read_file(self, file_path: Path) -> str:
        try:
            async with aiofiles.open(file_path, encoding="utf-8") as f:
                content = await f.read()
        except UnicodeDecodeError as e:
            raise ToolError(f"Unicode decode error reading {file_path}: {e}") from e
        except PermissionError:
            raise ToolError(f"Permission denied reading file: {file_path}")
        except Exception as e:
            raise ToolError(f"Unexpected error reading {file_path}: {e}") from e

        if len(content) > self.config.max_file_size:
            raise ToolError(
                f"File size ({len(content)} bytes) exceeds max_file_size "
                f"({self.config.max_file_size} bytes)"
            )

        return content

    async def _write_file(self, file_path: Path, content: str) -> None:
        try:
            async with aiofiles.open(file_path, mode="w", encoding="utf-8") as f:
                await f.write(content)
        except PermissionError:
            raise ToolError(f"Permission denied writing to file: {file_path}")
        except OSError as e:
            raise ToolError(f"OS error writing to {file_path}: {e}") from e
        except Exception as e:
            raise ToolError(f"Unexpected error writing to {file_path}: {e}") from e
