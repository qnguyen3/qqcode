from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MCPToolResult(BaseModel):
    """Normalized result of an MCP tool call."""

    ok: bool = True
    server: str
    tool: str
    text: str | None = None
    structured: dict[str, Any] | None = None

    def format_for_model(self) -> str:
        if self.structured is not None:
            return f"structured: {self.structured}"
        return self.text or ""


class RemoteTool(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        validation_alias="inputSchema",
    )

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("MCP tool missing valid 'name'")
        return v

    @field_validator("input_schema", mode="before")
    @classmethod
    def _normalize_schema(cls, v: Any) -> dict[str, Any]:
        if v is None:
            return {"type": "object", "properties": {}}
        if isinstance(v, dict):
            return v
        dump = getattr(v, "model_dump", None)
        if callable(dump):
            try:
                v = dump()
            except Exception:
                return {"type": "object", "properties": {}}
        return v if isinstance(v, dict) else {"type": "object", "properties": {}}


class RemoteResource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uri: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, validation_alias="mimeType")

    @field_validator("uri", mode="before")
    @classmethod
    def _coerce_uri(cls, v: Any) -> str:
        return str(v)


class RemoteResourceTemplate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uri_template: str = Field(validation_alias="uriTemplate")
    name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, validation_alias="mimeType")

    @field_validator("uri_template", mode="before")
    @classmethod
    def _coerce_uri_template(cls, v: Any) -> str:
        return str(v)


class ResourceContent(BaseModel):
    """Result of reading a single MCP resource."""

    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob_size: int | None = None
    blob_path: str | None = None

    def format_for_model(self) -> str:
        parts = [f"uri: {self.uri}"]
        if self.mime_type:
            parts.append(f"mimeType: {self.mime_type}")
        if self.text is not None:
            parts.append(f"text: {self.text}")
        if self.blob_path is not None:
            parts.append(
                f"binary content ({self.blob_size} bytes) saved to: {self.blob_path}"
            )
        return "\n".join(parts)


class _MCPContentBlock(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    text: str | None = None


class _MCPResultIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    structuredContent: dict[str, Any] | None = None
    content: list[_MCPContentBlock] | None = None

    @field_validator("structuredContent", mode="before")
    @classmethod
    def _normalize_structured(cls, v: Any) -> dict[str, Any] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        dump = getattr(v, "model_dump", None)
        if callable(dump):
            try:
                v = dump()
            except Exception:
                return None
        return v if isinstance(v, dict) else None


def parse_call_result(server: str, tool: str, result_obj: Any) -> MCPToolResult:
    parsed = _MCPResultIn.model_validate(result_obj)
    if (structured := parsed.structuredContent) is not None:
        return MCPToolResult(server=server, tool=tool, text=None, structured=structured)

    blocks = parsed.content or []
    parts = [b.text for b in blocks if isinstance(b.text, str)]
    text = "\n".join(parts) if parts else None
    return MCPToolResult(server=server, tool=tool, text=text, structured=None)
