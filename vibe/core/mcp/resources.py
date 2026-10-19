from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from vibe.core.mcp.client import MCPServerClient
from vibe.core.mcp.protocol import RemoteResource, RemoteResourceTemplate
from vibe.core.tools.base import BaseTool, BaseToolResult, BaseToolState, ToolError
from vibe.core.tools.mcp import MCPToolConfig
from vibe.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_notice,
    spill_to_temp_file,
    truncate_head,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay
from vibe.core.types import ToolCallEvent, ToolResultEvent

LIST_RESOURCES_SUFFIX = "list_resources"
LIST_TEMPLATES_SUFFIX = "list_resource_templates"
READ_RESOURCE_SUFFIX = "read_resource"


class _EmptyArgs(BaseModel):
    pass


class _ReadResourceArgs(BaseModel):
    uri: str = Field(description="URI of the MCP resource to read")


class MCPResourceEntry(BaseModel):
    uri: str
    name: str | None = None
    mime_type: str | None = None
    description: str | None = None


class MCPResourceTemplateEntry(BaseModel):
    uri_template: str
    name: str | None = None
    mime_type: str | None = None
    description: str | None = None


class MCPResourceListResult(BaseToolResult):
    server: str
    resources: list[MCPResourceEntry] = Field(default_factory=list)

    def format_for_model(self) -> str:
        if not self.resources:
            return f"MCP server '{self.server}' exposes no resources."
        lines = [f"Resources on MCP server '{self.server}':"]
        for item in self.resources:
            detail = f" ({item.mime_type})" if item.mime_type else ""
            label = item.name or item.uri
            lines.append(f"- {label}{detail}: {item.uri}")
        return "\n".join(lines)


class MCPResourceTemplateListResult(BaseToolResult):
    server: str
    templates: list[MCPResourceTemplateEntry] = Field(default_factory=list)

    def format_for_model(self) -> str:
        if not self.templates:
            return f"MCP server '{self.server}' exposes no resource templates."
        lines = [f"Resource templates on MCP server '{self.server}':"]
        for item in self.templates:
            detail = f" ({item.mime_type})" if item.mime_type else ""
            lines.append(
                f"- {item.name or item.uri_template}{detail}: {item.uri_template}"
            )
        return "\n".join(lines)


class MCPReadResourceResult(BaseToolResult):
    server: str
    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob_path: str | None = None
    blob_size: int | None = None

    def format_for_model(self) -> str:
        parts = [f"MCP resource {self.uri} from '{self.server}'"]
        if self.mime_type:
            parts.append(f"mimeType: {self.mime_type}")
        if self.text is not None:
            parts.append(self.text)
        if self.blob_path is not None:
            parts.append(
                f"Binary content ({self.blob_size} bytes) saved to: {self.blob_path}"
            )
        return "\n".join(parts)


def _tool_base_name(client: MCPServerClient) -> str:
    return f"mcp__{client.name}__"


def _server_label(client: MCPServerClient) -> str:
    hint = client.config.prompt
    return f"[{client.name}]" + (f" {hint}" if hint else "")


def _truncate_text(text: str, *, prefix: str) -> str:
    result = truncate_head(
        text, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES
    )
    if not result.truncated:
        return text
    path = spill_to_temp_file(text, prefix=prefix)
    return f"{result.content}\n{format_notice(replace(result, full_output_path=path))}"


def _make_list_resources_tool(
    client: MCPServerClient,
) -> type[BaseTool[_EmptyArgs, MCPResourceListResult, MCPToolConfig, BaseToolState]]:
    published_name = f"{_tool_base_name(client)}{LIST_RESOURCES_SUFFIX}"

    class MCPListResourcesTool(
        BaseTool[_EmptyArgs, MCPResourceListResult, MCPToolConfig, BaseToolState]
    ):
        description: ClassVar[str] = (
            f"List the resources exposed by MCP server '{client.name}'. "
            f"Read-only. {_server_label(client)}"
        )

        @classmethod
        def get_name(cls) -> str:
            return published_name

        async def run(self, args: _EmptyArgs) -> MCPResourceListResult:
            try:
                resources: list[RemoteResource] = await client.list_resources()
            except Exception as exc:
                raise ToolError(
                    f"Listing resources from MCP server '{client.name}' failed: {exc}"
                ) from exc
            entries = [
                MCPResourceEntry(
                    uri=resource.uri,
                    name=resource.name,
                    mime_type=resource.mime_type,
                    description=resource.description,
                )
                for resource in resources
            ]
            return MCPResourceListResult(server=client.name, resources=entries)

        @classmethod
        def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
            return ToolCallDisplay(summary=published_name)

        @classmethod
        def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
            if not isinstance(event.result, MCPResourceListResult):
                return ToolResultDisplay(
                    success=False,
                    message=event.error or event.skip_reason or "No result",
                )
            count = len(event.result.resources)
            return ToolResultDisplay(
                success=True, message=f"{count} resource(s) on {client.name}"
            )

        @classmethod
        def get_status_text(cls) -> str:
            return f"Listing resources on {client.name}"

    MCPListResourcesTool.__name__ = f"MCP_{client.name}__list_resources"
    return MCPListResourcesTool


def _make_list_templates_tool(
    client: MCPServerClient,
) -> type[
    BaseTool[_EmptyArgs, MCPResourceTemplateListResult, MCPToolConfig, BaseToolState]
]:
    published_name = f"{_tool_base_name(client)}{LIST_TEMPLATES_SUFFIX}"

    class MCPListResourceTemplatesTool(
        BaseTool[
            _EmptyArgs, MCPResourceTemplateListResult, MCPToolConfig, BaseToolState
        ]
    ):
        description: ClassVar[str] = (
            f"List the resource templates exposed by MCP server '{client.name}'. "
            f"Read-only. {_server_label(client)}"
        )

        @classmethod
        def get_name(cls) -> str:
            return published_name

        async def run(self, args: _EmptyArgs) -> MCPResourceTemplateListResult:
            try:
                templates: list[
                    RemoteResourceTemplate
                ] = await client.list_resource_templates()
            except Exception as exc:
                raise ToolError(
                    f"Listing resource templates from MCP server "
                    f"'{client.name}' failed: {exc}"
                ) from exc
            entries = [
                MCPResourceTemplateEntry(
                    uri_template=template.uri_template,
                    name=template.name,
                    mime_type=template.mime_type,
                    description=template.description,
                )
                for template in templates
            ]
            return MCPResourceTemplateListResult(server=client.name, templates=entries)

        @classmethod
        def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
            return ToolCallDisplay(summary=published_name)

        @classmethod
        def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
            if not isinstance(event.result, MCPResourceTemplateListResult):
                return ToolResultDisplay(
                    success=False,
                    message=event.error or event.skip_reason or "No result",
                )
            count = len(event.result.templates)
            return ToolResultDisplay(
                success=True, message=f"{count} template(s) on {client.name}"
            )

        @classmethod
        def get_status_text(cls) -> str:
            return f"Listing resource templates on {client.name}"

    MCPListResourceTemplatesTool.__name__ = f"MCP_{client.name}__list_templates"
    return MCPListResourceTemplatesTool


def _make_read_resource_tool(
    client: MCPServerClient,
) -> type[
    BaseTool[_ReadResourceArgs, MCPReadResourceResult, MCPToolConfig, BaseToolState]
]:
    published_name = f"{_tool_base_name(client)}{READ_RESOURCE_SUFFIX}"

    class MCPReadResourceTool(
        BaseTool[_ReadResourceArgs, MCPReadResourceResult, MCPToolConfig, BaseToolState]
    ):
        description: ClassVar[str] = (
            f"Read a resource (by URI) from MCP server '{client.name}'. "
            f"Read-only; output is truncated with the full content saved to a "
            f"temporary file. {_server_label(client)}"
        )

        @classmethod
        def get_name(cls) -> str:
            return published_name

        async def run(self, args: _ReadResourceArgs) -> MCPReadResourceResult:
            try:
                content = await client.read_resource(args.uri)
            except Exception as exc:
                raise ToolError(
                    f"Reading resource '{args.uri}' from MCP server "
                    f"'{client.name}' failed: {exc}"
                ) from exc

            if content.blob_path is not None:
                return MCPReadResourceResult(
                    server=client.name,
                    uri=content.uri,
                    mime_type=content.mime_type,
                    blob_path=content.blob_path,
                    blob_size=content.blob_size,
                )

            raw_text = content.text or ""
            text = _truncate_text(raw_text, prefix=f"qqcode-mcp-{client.name}-resource")
            return MCPReadResourceResult(
                server=client.name,
                uri=content.uri,
                mime_type=content.mime_type,
                text=text,
                truncated=text != raw_text,
            )

        @classmethod
        def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
            uri = getattr(event.args, "uri", "")
            return ToolCallDisplay(summary=f"{published_name}", details={"uri": uri})

        @classmethod
        def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
            if not isinstance(event.result, MCPReadResourceResult):
                return ToolResultDisplay(
                    success=False,
                    message=event.error or event.skip_reason or "No result",
                )
            return ToolResultDisplay(success=True, message=f"Read {event.result.uri}")

        @classmethod
        def get_status_text(cls) -> str:
            return f"Reading resource from {client.name}"

    MCPReadResourceTool.__name__ = f"MCP_{client.name}__read_resource"
    return MCPReadResourceTool


def create_resource_tool_classes(
    client: MCPServerClient,
) -> list[type[BaseTool[Any, Any, MCPToolConfig, BaseToolState]]]:
    """Create the three read-only resource tools for one MCP server."""
    return [
        _make_list_resources_tool(client),
        _make_list_templates_tool(client),
        _make_read_resource_tool(client),
    ]
