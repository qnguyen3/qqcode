from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vibe.core.tools.ui import ToolResultDisplay

from vibe.cli.textual_ui.widgets.tool_widgets import (
    BashApprovalWidget,
    BashResultWidget,
    EditApprovalWidget,
    EditResultWidget,
    GrepApprovalWidget,
    GrepResultWidget,
    ReadFileApprovalWidget,
    ReadFileResultWidget,
    TodoApprovalWidget,
    TodoResultWidget,
    ToolApprovalWidget,
    ToolResultWidget,
    WriteFileApprovalWidget,
    WriteFileResultWidget,
)


class ToolRenderer:
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        return ToolApprovalWidget, tool_args

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[ToolResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "details": self._clean_details(display.details),
            "warnings": display.warnings,
        }
        return ToolResultWidget, data

    def _clean_details(self, details: dict) -> dict:
        clean = {}
        for key, value in details.items():
            if value is None or value in ("", []):
                continue
            value_str = str(value).strip().replace("\n", " ").replace("\r", "")
            value_str = " ".join(value_str.split())
            if value_str:
                clean[key] = value_str
        return clean


class BashRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[BashApprovalWidget], dict[str, Any]]:
        data = {
            "command": tool_args.get("command", ""),
            "description": tool_args.get("description", ""),
        }
        return BashApprovalWidget, data

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[BashResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "details": self._clean_details(display.details),
            "warnings": display.warnings,
        }
        return BashResultWidget, data


class WriteFileRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[WriteFileApprovalWidget], dict[str, Any]]:
        data = {
            "path": tool_args.get("path", ""),
            "content": tool_args.get("content", ""),
            "file_extension": tool_args.get("file_extension", "text"),
        }
        return WriteFileApprovalWidget, data

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[WriteFileResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "path": display.details.get("path", ""),
            "bytes_written": display.details.get("bytes_written"),
            "content": display.details.get("content", ""),
            "file_extension": display.details.get("file_extension", "text"),
        }
        return WriteFileResultWidget, data


class EditRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[EditApprovalWidget], dict[str, Any]]:
        file_path = tool_args.get("file_path", "")
        old_string = tool_args.get("old_string", "")
        new_string = tool_args.get("new_string", "")

        diff_lines = self._create_diff(old_string, new_string)

        data = {"file_path": file_path, "diff_lines": diff_lines}
        return EditApprovalWidget, data

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[EditResultWidget], dict[str, Any]]:
        old_string = display.details.get("old_string", "")
        new_string = display.details.get("new_string", "")
        diff_lines = self._create_diff(old_string, new_string) if not collapsed else []
        data = {
            "success": display.success,
            "message": display.message,
            "diff_lines": diff_lines,
        }
        return EditResultWidget, data

    def _create_diff(self, old_string: str, new_string: str) -> list[str]:
        old_lines = old_string.split("\n")
        new_lines = new_string.split("\n")

        diff = difflib.unified_diff(old_lines, new_lines, lineterm="", n=2)
        return list(diff)[2:]  # Skip file headers


class TodoRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[TodoApprovalWidget], dict[str, Any]]:
        data = {"description": tool_args.get("description", "")}
        return TodoApprovalWidget, data

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[TodoResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "todos_by_status": display.details.get("todos_by_status", {}),
        }
        return TodoResultWidget, data


class ReadFileRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[ReadFileApprovalWidget], dict[str, Any]]:
        return ReadFileApprovalWidget, tool_args

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[ReadFileResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "path": display.details.get("path", ""),
            "warnings": display.warnings,
            "content": display.details.get("content", "") if not collapsed else "",
            "file_extension": display.details.get("file_extension", "text"),
        }
        return ReadFileResultWidget, data


class GrepRenderer(ToolRenderer):
    def get_approval_widget(
        self, tool_args: dict
    ) -> tuple[type[GrepApprovalWidget], dict[str, Any]]:
        return GrepApprovalWidget, tool_args

    def get_result_widget(
        self, display: ToolResultDisplay, collapsed: bool
    ) -> tuple[type[GrepResultWidget], dict[str, Any]]:
        data = {
            "success": display.success,
            "message": display.message,
            "warnings": display.warnings,
            "matches": display.details.get("matches", "") if not collapsed else "",
        }
        return GrepResultWidget, data


_RENDERER_REGISTRY: dict[str, type[ToolRenderer]] = {
    "write_file": WriteFileRenderer,
    "edit": EditRenderer,
    "todo": TodoRenderer,
    "read_file": ReadFileRenderer,
    "bash": BashRenderer,
    "grep": GrepRenderer,
}


def get_renderer(tool_name: str) -> ToolRenderer:
    renderer_class = _RENDERER_REGISTRY.get(tool_name, ToolRenderer)
    return renderer_class()
