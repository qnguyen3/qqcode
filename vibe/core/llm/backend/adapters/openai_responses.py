from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from vibe.core.config import ThinkingLevel, extra_body_has_reasoning
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


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
        )
    return content if content is not None else ""


@register_adapter("openai-responses")
class OpenAIResponsesAdapter(APIAdapter):
    """Adapter for OpenAI's /responses API.

    Translates chat-style messages to Responses ``input`` items, keeps the
    same prompt-cache policy as the chat adapter and maps reasoning summaries
    back to ``LLMMessage.reasoning_content``.
    """

    endpoint: ClassVar[str] = "/responses"

    def build_headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _convert_tool(self, tool: AvailableTool) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.function.name,
            "description": tool.function.description or "",
            "parameters": tool.function.parameters
            or {"type": "object", "properties": {}},
        }

    def _convert_tool_choice(
        self, tool_choice: StrToolChoice | AvailableTool | None
    ) -> Any:
        match tool_choice:
            case None:
                return None
            case "auto" | "required" | "none":
                return tool_choice
            case "any":
                return "required"
            case AvailableTool():
                return {"type": "function", "name": tool_choice.function.name}
            case str():
                return "auto"

    def _convert_messages(
        self, converted_messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []

        for msg in converted_messages:
            role = msg.get("role", "")
            match role:
                case "system":
                    if text := _as_text(msg.get("content")):
                        instructions.append(text)
                case "user":
                    input_items.append({
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _as_text(msg.get("content"))}
                        ],
                    })
                case "assistant":
                    if text := _as_text(msg.get("content")):
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                    for tc in msg.get("tool_calls") or []:
                        func = tc.get("function", {})
                        arguments = func.get("arguments")
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments or {})
                        input_items.append({
                            "type": "function_call",
                            "call_id": tc.get("id") or "",
                            "name": func.get("name") or "",
                            "arguments": arguments,
                        })
                case "tool":
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id") or "",
                        "output": _as_text(msg.get("content")),
                    })

        return "\n".join(instructions), input_items

    def build_payload(
        self,
        model_name: str,
        converted_messages: list[dict[str, Any]],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        instructions, input_items = self._convert_messages(converted_messages)

        payload: dict[str, Any] = {
            "model": model_name,
            "input": input_items,
            "temperature": temperature,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [self._convert_tool(tool) for tool in tools]
        if converted_choice := self._convert_tool_choice(tool_choice):
            payload["tool_choice"] = converted_choice
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        if cache_plan and cache_plan.enabled:
            extra = extra_body or {}
            if "prompt_cache_key" not in extra and cache_plan.session_key:
                payload["prompt_cache_key"] = cache_plan.session_key
            if "prompt_cache_retention" not in extra and cache_plan.ttl == "1h":
                payload["prompt_cache_retention"] = "24h"

        if (
            effort := effective_reasoning_effort(thinking_level, reasoning_effort)
        ) is not None and not extra_body_has_reasoning(extra_body):
            payload["reasoning"] = {"effort": effort}

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
        converted_messages = [msg.model_dump(exclude_none=True) for msg in messages]

        payload = self.build_payload(
            model_name,
            converted_messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            extra_body,
            cache_plan,
            thinking_level,
            reasoning_effort=reasoning_effort,
        )

        if enable_streaming:
            payload["stream"] = True

        headers = self.build_headers(api_key)
        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(self.endpoint, headers, body)

    @staticmethod
    def _parse_usage(usage_data: dict[str, Any]) -> LLMUsage:
        details = usage_data.get("input_tokens_details") or {}
        cache_read_tokens = details.get("cached_tokens") or 0
        return LLMUsage(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=0,
        )

    @staticmethod
    def _parse_output(output: list[dict[str, Any]]) -> tuple[str, str, list[ToolCall]]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in output:
            match item.get("type", ""):
                case "message":
                    for part in item.get("content") or []:
                        if part.get("type") == "output_text":
                            text_parts.append(part.get("text", ""))
                case "reasoning":
                    for summary in item.get("summary") or []:
                        reasoning_parts.append(summary.get("text", ""))
                case "function_call":
                    tool_calls.append(
                        ToolCall(
                            id=item.get("call_id"),
                            function=FunctionCall(
                                name=item.get("name"),
                                arguments=item.get("arguments")
                                if isinstance(item.get("arguments"), str)
                                else json.dumps(item.get("arguments") or {}),
                            ),
                        )
                    )

        return "".join(text_parts), "".join(reasoning_parts), tool_calls

    def _parse_complete(self, data: dict[str, Any]) -> LLMChunk:
        text, reasoning, tool_calls = self._parse_output(data.get("output") or [])
        message = LLMMessage(
            role=Role.assistant,
            content=text or None,
            reasoning_content=reasoning or None,
            tool_calls=tool_calls or None,
        )
        return LLMChunk(
            message=message,
            usage=self._parse_usage(data.get("usage") or {}),
            finish_reason=data.get("status"),
        )

    def _parse_event(self, data: dict[str, Any]) -> LLMChunk:
        event_type = data.get("type", "")

        match event_type:
            case "response.output_text.delta":
                return LLMChunk(
                    message=LLMMessage(
                        role=Role.assistant, content=data.get("delta") or ""
                    ),
                    usage=_EMPTY_USAGE,
                )
            case (
                "response.reasoning_summary_text.delta"
                | "response.reasoning_text.delta"
            ):
                return LLMChunk(
                    message=LLMMessage(
                        role=Role.assistant, reasoning_content=data.get("delta") or ""
                    ),
                    usage=_EMPTY_USAGE,
                )
            case "response.output_item.added":
                item = data.get("item") or {}
                if item.get("type") == "function_call":
                    return LLMChunk(
                        message=LLMMessage(
                            role=Role.assistant,
                            tool_calls=[
                                ToolCall(
                                    id=item.get("call_id"),
                                    index=data.get("output_index"),
                                    function=FunctionCall(
                                        name=item.get("name"), arguments=""
                                    ),
                                )
                            ],
                        ),
                        usage=_EMPTY_USAGE,
                    )
            case "response.function_call_arguments.delta":
                return LLMChunk(
                    message=LLMMessage(
                        role=Role.assistant,
                        tool_calls=[
                            ToolCall(
                                index=data.get("output_index"),
                                function=FunctionCall(
                                    arguments=data.get("delta") or ""
                                ),
                            )
                        ],
                    ),
                    usage=_EMPTY_USAGE,
                )
            case "response.completed":
                response = data.get("response") or {}
                return LLMChunk(
                    message=LLMMessage(role=Role.assistant, content=""),
                    usage=self._parse_usage(response.get("usage") or {}),
                    finish_reason=response.get("status"),
                )
            case "response.failed" | "error":
                error = data.get("error") or data.get("response", {}).get("error")
                raise ValueError(f"OpenAI responses stream failed: {error}")

        return LLMChunk(
            message=LLMMessage(role=Role.assistant, content=""), usage=_EMPTY_USAGE
        )

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        match data.get("type"):
            case str() as event_type if event_type:
                return self._parse_event(data)
            case _:
                return self._parse_complete(data)
