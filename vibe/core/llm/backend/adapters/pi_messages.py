from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, ClassVar

from vibe.core.config import ThinkingLevel
from vibe.core.llm.backend.adapters.base import (
    APIAdapter,
    PreparedRequest,
    effective_reasoning_effort,
    register_adapter,
)
from vibe.core.llm.cache import CachePlan
from vibe.core.types import (
    AvailableTool,
    FunctionCall,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
    ToolCall,
)

if TYPE_CHECKING:
    from vibe.core.config import ProviderConfig

_EMPTY_USAGE = LLMUsage(prompt_tokens=0, completion_tokens=0)
_STOP_REASONS = {"stop": "stop", "length": "length", "toolUse": "tool_calls"}
_TOOL_CHOICES = {
    "auto": "auto",
    "none": "none",
    "any": "required",
    "required": "required",
}
_ZERO_PI_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@register_adapter("pi-messages")
class PiMessagesAdapter(APIAdapter):
    """Adapter for the streaming ``pi-messages`` protocol used by Radius.

    The wire protocol is SSE-only: the request posts ``{model, context,
    options}`` to ``/messages`` and the response is a stream of assistant
    message events. Non-streaming callers are served by aggregating the stream.
    """

    endpoint: ClassVar[str] = "/messages"
    streaming_only: ClassVar[bool] = True

    def build_headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _convert_tool(tool: AvailableTool) -> dict[str, Any]:
        return {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "parameters": tool.function.parameters or {"type": "object"},
        }

    @staticmethod
    def _convert_tool_choice(tool_choice: StrToolChoice | AvailableTool | None) -> Any:
        match tool_choice:
            case None:
                return None
            case "auto" | "none" | "any" | "required":
                return _TOOL_CHOICES[tool_choice]
            case AvailableTool():
                return {
                    "type": "function",
                    "function": {"name": tool_choice.function.name},
                }

    @staticmethod
    def _convert_content(message: LLMMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {"type": "image", "data": image.data, "mimeType": image.media_type}
            for image in message.images
            if image.is_loaded
        )
        return blocks

    def _convert_message(
        self, message: LLMMessage, model_name: str, provider_name: str
    ) -> dict[str, Any] | None:
        match message.role:
            case Role.user:
                return {
                    "role": "user",
                    "content": self._convert_content(message)
                    or [{"type": "text", "text": ""}],
                    "timestamp": _now_ms(),
                }
            case Role.assistant:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "toolCall",
                        "id": tool_call.id or "",
                        "name": tool_call.function.name or "",
                        "arguments": self._tool_arguments(tool_call),
                    }
                    for tool_call in message.tool_calls or []
                )
                if not blocks:
                    return None
                return {
                    "role": "assistant",
                    "content": blocks,
                    "api": "pi-messages",
                    "provider": provider_name,
                    "model": model_name,
                    "usage": _ZERO_PI_USAGE,
                    "stopReason": "stop",
                    "timestamp": _now_ms(),
                }
            case Role.tool:
                return {
                    "role": "toolResult",
                    "toolCallId": message.tool_call_id or "",
                    "toolName": message.name or "",
                    "content": self._convert_content(message)
                    or [{"type": "text", "text": ""}],
                    "isError": False,
                    "timestamp": _now_ms(),
                }
            case _:
                return None

    @staticmethod
    def _tool_arguments(tool_call: ToolCall) -> dict[str, Any]:
        try:
            parsed = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _convert_context(
        self,
        messages: list[LLMMessage],
        tools: list[AvailableTool] | None,
        model_name: str,
        provider_name: str,
    ) -> dict[str, Any]:
        system_text = "\n".join(
            message.content or ""
            for message in messages
            if message.role is Role.system and message.content
        ).strip()
        converted = [
            converted
            for message in messages
            if message.role is not Role.system
            and (converted := self._convert_message(message, model_name, provider_name))
            is not None
        ]

        pi_messages: list[dict[str, Any]] = []
        if system_text or tools:
            system: dict[str, Any] = {
                "role": "system",
                "content": system_text,
                "timestamp": _now_ms(),
            }
            if tools:
                system["toolsAdded"] = [self._convert_tool(tool) for tool in tools]
            pi_messages.append(system)
        pi_messages.extend(converted)
        return {"messages": pi_messages}

    def build_payload(
        self,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        provider_name: str = "",
        thinking_level: ThinkingLevel | None = None,
        extra_body: dict[str, Any] | None = None,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            options["maxTokens"] = max_tokens
        if tool_choice is not None:
            options["toolChoice"] = self._convert_tool_choice(tool_choice)
        if (
            effort := effective_reasoning_effort(thinking_level, reasoning_effort)
        ) is not None:
            options["reasoning"] = effort

        payload: dict[str, Any] = {
            "model": model_name,
            "context": self._convert_context(
                messages, tools, model_name, provider_name
            ),
            "options": options,
        }
        if extra_body:
            payload.update(extra_body)
        return payload

    def prepare_request(
        self,
        *,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        enable_streaming: bool,
        provider: ProviderConfig,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
        reasoning_effort: str | None = None,
    ) -> PreparedRequest:
        payload = self.build_payload(
            model_name,
            messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            provider_name=provider.name,
            thinking_level=thinking_level,
            extra_body=extra_body,
            reasoning_effort=reasoning_effort,
        )
        headers = self.build_headers(api_key)
        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(self.endpoint, headers, body)

    @staticmethod
    def _parse_usage(usage: dict[str, Any]) -> LLMUsage:
        return LLMUsage(
            prompt_tokens=usage.get("input", 0),
            completion_tokens=usage.get("output", 0),
            cache_read_tokens=usage.get("cacheRead", 0),
            cache_write_tokens=usage.get("cacheWrite", 0),
        )

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        event_type = data.get("type", "")
        message = LLMMessage(role=Role.assistant, content="")
        usage = _EMPTY_USAGE
        finish_reason: str | None = None

        match event_type:
            case "text_delta":
                message = LLMMessage(
                    role=Role.assistant, content=data.get("delta") or ""
                )
            case "thinking_delta":
                message = LLMMessage(
                    role=Role.assistant, reasoning_content=data.get("delta") or ""
                )
            case "toolcall_start":
                message = LLMMessage(
                    role=Role.assistant,
                    tool_calls=[
                        ToolCall(
                            id=data.get("id"),
                            index=data.get("contentIndex"),
                            function=FunctionCall(
                                name=data.get("toolName"), arguments=""
                            ),
                        )
                    ],
                )
            case "toolcall_delta":
                message = LLMMessage(
                    role=Role.assistant,
                    tool_calls=[
                        ToolCall(
                            index=data.get("contentIndex"),
                            function=FunctionCall(arguments=data.get("delta") or ""),
                        )
                    ],
                )
            case "done":
                usage = self._parse_usage(data.get("usage") or {})
                finish_reason = _STOP_REASONS.get(data.get("reason", ""))
            case "error":
                raise ValueError(
                    f"pi-messages stream failed: {data.get('errorMessage') or data}"
                )

        return LLMChunk(message=message, usage=usage, finish_reason=finish_reason)
