from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from vibe.core.config import DEFAULT_BUDGETS, ThinkingLevel
from vibe.core.llm.backend.adapters.base import (
    APIAdapter,
    PreparedRequest,
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
_SCHEMA_META_KEYS = frozenset({
    "$schema",
    "$id",
    "$anchor",
    "$dynamicAnchor",
    "$vocabulary",
    "$comment",
    "$defs",
    "definitions",
})
_STOP_REASONS = {"STOP": "stop", "MAX_TOKENS": "length"}
_TOOL_CHOICE_MODES = {"auto": "AUTO", "none": "NONE", "any": "ANY"}
_THINKING_LEVELS = {
    ThinkingLevel.MINIMAL: "MINIMAL",
    ThinkingLevel.LOW: "LOW",
    ThinkingLevel.MEDIUM: "MEDIUM",
    ThinkingLevel.HIGH: "HIGH",
}


def _sanitize_schema(schema: Any) -> Any:
    """Drop JSON Schema meta-declarations Gemini's OpenAPI subset rejects."""
    if not isinstance(schema, dict):
        return schema
    return {
        key: _sanitize_schema(value)
        for key, value in schema.items()
        if key not in _SCHEMA_META_KEYS
    }


def _uses_thinking_level(model_name: str) -> bool:
    """Gemini 3+ models select thinking with a discrete level, not a budget."""
    lowered = model_name.lower()
    return lowered.startswith("gemini-3") or lowered in {
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
    }


@register_adapter("google-generative-ai")
class GoogleGenerativeAIAdapter(APIAdapter):
    """Adapter for Google's native ``generateContent`` API."""

    endpoint: ClassVar[str] = "/models"

    def build_headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-goog-api-key"] = api_key
        return headers

    @staticmethod
    def _convert_tool(tool: AvailableTool) -> dict[str, Any]:
        parameters = tool.function.parameters or {"type": "object", "properties": {}}
        return {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "parametersJsonSchema": _sanitize_schema(parameters),
        }

    @staticmethod
    def _convert_tool_choice(
        tool_choice: StrToolChoice | AvailableTool | None,
    ) -> dict[str, Any] | None:
        match tool_choice:
            case "auto" | "none" | "any":
                return {
                    "functionCallingConfig": {"mode": _TOOL_CHOICE_MODES[tool_choice]}
                }
            case AvailableTool():
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_choice.function.name],
                    }
                }
            case _:
                return None

    def _convert_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for message in messages:
            match message.role:
                case Role.system:
                    if message.content:
                        system_parts.append(message.content)
                case Role.user:
                    parts: list[dict[str, Any]] = []
                    if message.content:
                        parts.append({"text": message.content})
                    parts.extend(
                        {
                            "inlineData": {
                                "mimeType": image.media_type,
                                "data": image.data,
                            }
                        }
                        for image in message.images
                        if image.is_loaded
                    )
                    contents.append({"role": "user", "parts": parts or [{"text": ""}]})
                case Role.assistant:
                    parts = []
                    if message.content:
                        parts.append({"text": message.content})
                    parts.extend(
                        self._convert_tool_call(tool_call)
                        for tool_call in message.tool_calls or []
                    )
                    if parts:
                        contents.append({"role": "model", "parts": parts})
                case Role.tool:
                    self._append_function_response(contents, message)

        return "\n".join(system_parts).strip(), contents

    @staticmethod
    def _convert_tool_call(tool_call: ToolCall) -> dict[str, Any]:
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        call: dict[str, Any] = {
            "name": tool_call.function.name or "",
            "args": args if isinstance(args, dict) else {},
        }
        if tool_call.id:
            call["id"] = tool_call.id
        return {"functionCall": call}

    @staticmethod
    def _append_function_response(
        contents: list[dict[str, Any]], message: LLMMessage
    ) -> None:
        response: dict[str, Any] = {
            "name": message.name or "",
            "response": {"output": message.content or ""},
        }
        if message.tool_call_id:
            response["id"] = message.tool_call_id
        part = {"functionResponse": response}

        if (
            contents
            and contents[-1]["role"] == "user"
            and any("functionResponse" in p for p in contents[-1]["parts"])
        ):
            contents[-1]["parts"].append(part)
            return
        contents.append({"role": "user", "parts": [part]})

    @staticmethod
    def _thinking_config(
        model_name: str, thinking_level: ThinkingLevel | None
    ) -> dict[str, Any] | None:
        match thinking_level:
            case None:
                return None
            case ThinkingLevel.OFF:
                return {"thinkingBudget": 0}
            case _ if _uses_thinking_level(model_name):
                return {
                    "includeThoughts": True,
                    "thinkingLevel": _THINKING_LEVELS[thinking_level],
                }
            case _:
                return {
                    "includeThoughts": True,
                    "thinkingBudget": DEFAULT_BUDGETS[thinking_level],
                }

    def build_payload(
        self,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> dict[str, Any]:
        system_instruction, contents = self._convert_messages(messages)

        generation_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if thinking := self._thinking_config(model_name, thinking_level):
            generation_config["thinkingConfig"] = thinking

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if tools:
            payload["tools"] = [
                {"functionDeclarations": [self._convert_tool(t) for t in tools]}
            ]
        if tool_config := self._convert_tool_choice(tool_choice):
            payload["toolConfig"] = tool_config
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
            extra_body,
            thinking_level,
        )

        action = (
            "streamGenerateContent?alt=sse" if enable_streaming else "generateContent"
        )
        endpoint = f"{self.endpoint}/{model_name}:{action}"
        headers = self.build_headers(api_key)
        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(endpoint, headers, body)

    @staticmethod
    def _parse_parts(parts: list[dict[str, Any]]) -> tuple[str, list[ToolCall]]:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            if text := part.get("text"):
                text_parts.append(text)
            if call := part.get("functionCall"):
                tool_calls.append(
                    ToolCall(
                        id=call.get("id"),
                        function=FunctionCall(
                            name=call.get("name"),
                            arguments=json.dumps(call.get("args") or {}),
                        ),
                    )
                )
        return "".join(text_parts), tool_calls

    @staticmethod
    def _parse_usage(usage: dict[str, Any]) -> LLMUsage:
        cache_read = usage.get("cachedContentTokenCount") or 0
        return LLMUsage(
            prompt_tokens=max((usage.get("promptTokenCount") or 0) - cache_read, 0),
            completion_tokens=(usage.get("candidatesTokenCount") or 0)
            + (usage.get("thoughtsTokenCount") or 0),
            cache_read_tokens=cache_read,
            cache_write_tokens=0,
        )

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        usage = self._parse_usage(data.get("usageMetadata") or {})
        candidates = data.get("candidates") or []
        if not candidates:
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=""), usage=usage
            )

        candidate = candidates[0]
        content = candidate.get("content") or {}
        text, tool_calls = self._parse_parts(content.get("parts") or [])
        finish_reason = _STOP_REASONS.get(candidate.get("finishReason", ""))
        message = LLMMessage(
            role=Role.assistant, content=text or None, tool_calls=tool_calls or None
        )
        return LLMChunk(message=message, usage=usage, finish_reason=finish_reason)
