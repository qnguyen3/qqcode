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
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
)

if TYPE_CHECKING:
    from vibe.core.config import ProviderConfig


@register_adapter("anthropic")
class AnthropicAdapter(APIAdapter):
    """Adapter for Anthropic's native /messages API."""

    endpoint: ClassVar[str] = "/messages"

    @staticmethod
    def _cache_control(ttl: str | None) -> dict[str, str]:
        control = {"type": "ephemeral"}
        if ttl == "1h":
            control["ttl"] = "1h"
        return control

    def _apply_cache_control(self, payload: dict[str, Any], ttl: str | None) -> None:
        cache_control = self._cache_control(ttl)
        system = payload.get("system")
        if system:
            system[-1] = {**system[-1], "cache_control": cache_control}

        messages = payload.get("messages") or []
        if len(messages) <= 1:
            return
        target = messages[-2]
        content = target.get("content")
        if not isinstance(content, list):
            return
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if not isinstance(block, dict):
                continue
            if block.get("type") not in {"text", "tool_result"}:
                continue
            content[index] = {**block, "cache_control": cache_control}
            messages[-2] = {**target, "content": content}
            return

    @staticmethod
    def _select_thinking(
        extra_body: dict[str, Any] | None, thinking_level: ThinkingLevel | None
    ) -> dict[str, Any] | None:
        if extra_body and "thinking" in extra_body:
            thinking = extra_body["thinking"]
            return thinking if isinstance(thinking, dict) else None
        if thinking_level is None or thinking_level is ThinkingLevel.OFF:
            return None
        return {"type": "enabled", "budget_tokens": DEFAULT_BUDGETS[thinking_level]}

    @staticmethod
    def _convert_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
        func = tool_call.get("function", {})
        input_data = func.get("arguments", "{}")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                input_data = {}
        return {
            "type": "tool_use",
            "id": tool_call.get("id", ""),
            "name": func.get("name", ""),
            "input": input_data,
        }

    def _convert_assistant_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        content = msg.get("content", "")
        if not msg.get("tool_calls"):
            return {
                "role": "assistant",
                "content": [{"type": "text", "text": content}]
                if content
                else [{"type": "text", "text": ""}],
            }
        blocks: list[dict[str, Any]] = []
        if content:
            blocks.append({"type": "text", "text": content})
        blocks.extend(self._convert_tool_call(tc) for tc in msg["tool_calls"])
        return {"role": "assistant", "content": blocks}

    @staticmethod
    def _append_tool_result(
        messages: list[dict[str, Any]], tool_result: dict[str, Any]
    ) -> None:
        if messages and messages[-1]["role"] == "user":
            last_content = messages[-1]["content"]
            if isinstance(last_content, list):
                last_content.append(tool_result)
                return
            messages[-1]["content"] = [
                block
                for block in (
                    {"type": "text", "text": last_content} if last_content else None,
                    tool_result,
                )
                if block
            ]
            return
        messages.append({"role": "user", "content": [tool_result]})

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
    ) -> dict[str, Any]:
        # Convert OpenAI-style messages to Anthropic format
        system_content = ""
        anthropic_messages = []

        for msg in converted_messages:
            match msg.get("role", ""):
                case "system":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        system_content += content + "\n"
                case "assistant":
                    anthropic_messages.append(self._convert_assistant_message(msg))
                case "user":
                    content = msg.get("content", "")
                    blocks: list[dict[str, Any]] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for image in msg.get("images") or []:
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image.get("media_type", "image/png"),
                                "data": image.get("data", ""),
                            },
                        })
                    anthropic_messages.append({
                        "role": "user",
                        "content": blocks or [{"type": "text", "text": ""}],
                    })
                case "tool":
                    self._append_tool_result(
                        anthropic_messages,
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": msg.get("content", ""),
                        },
                    )

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 8192,
        }

        if system_content.strip():
            # System must be array of text blocks
            payload["system"] = [{"type": "text", "text": system_content.strip()}]

        if temperature > 0:
            payload["temperature"] = temperature

        if tools:
            payload["tools"] = [self._convert_tool(t) for t in tools]

        if converted_choice := self._convert_tool_choice(tool_choice):
            payload["tool_choice"] = converted_choice

        if cache_plan and cache_plan.enabled:
            self._apply_cache_control(payload, cache_plan.ttl)
        if extra_body:
            payload.update(extra_body)

        # Thinking disables temperature; the user's extra_body wins over the level.
        if thinking := self._select_thinking(extra_body, thinking_level):
            payload["thinking"] = thinking
            payload.pop("temperature", None)

        return payload

    def _convert_tool(self, tool: AvailableTool) -> dict[str, Any]:
        """Convert OpenAI tool format to Anthropic format."""
        return {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "input_schema": tool.function.parameters
            or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _convert_tool_choice(
        tool_choice: StrToolChoice | AvailableTool | None,
    ) -> dict[str, Any] | None:
        match tool_choice:
            case "auto":
                return {"type": "auto"}
            case "required":
                return {"type": "any"}
            case "none" | None:
                return None
            case AvailableTool():
                return {"type": "tool", "name": tool_choice.function.name}
            case _:
                return None

    def build_headers(
        self, api_key: str | None = None, is_oauth: bool = False
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            if is_oauth:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                headers["x-api-key"] = api_key
        return headers

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
        # Check if OAuth is being used - need to inject Claude Code prefix
        is_oauth = provider.oauth_token is not None
        messages_to_send = list(messages)

        if is_oauth:
            # Claude Code OAuth requires this specific system prompt prefix
            claude_code_prefix = (
                "You are Claude Code, Anthropic's official CLI for Claude."
            )
            # Prepend the required prefix to identify as Claude Code
            prefix_msg = LLMMessage(role=Role.system, content=claude_code_prefix)
            messages_to_send = [prefix_msg, *messages_to_send]

        converted_messages = [
            msg.model_dump(exclude_none=True) for msg in messages_to_send
        ]

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
        )

        if enable_streaming:
            payload["stream"] = True

        headers = self.build_headers(api_key, is_oauth=is_oauth)

        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(self.endpoint, headers, body)

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        # Handle streaming events
        event_type = data.get("type", "")

        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            text = delta.get("text", "")
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=text),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )

        if event_type == "message_delta":
            usage_data = data.get("usage", {})
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=""),
                usage=LLMUsage(
                    prompt_tokens=0,
                    completion_tokens=usage_data.get("output_tokens", 0),
                ),
                finish_reason=data.get("delta", {}).get("stop_reason"),
            )

        if event_type in {
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_stop",
            "ping",
        }:
            # These are metadata events, return empty chunk
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=""),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )

        # Handle non-streaming response (full message)
        content_blocks = data.get("content", [])
        text_content = ""
        tool_calls = []

        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                text_content += block.get("text", "")
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        message = LLMMessage(
            role=Role.assistant,
            content=text_content if text_content else None,
            tool_calls=tool_calls if tool_calls else None,
        )

        usage_data = data.get("usage", {})
        cache_read_tokens = usage_data.get("cache_read_input_tokens") or 0
        cache_write_tokens = usage_data.get("cache_creation_input_tokens") or 0
        usage = LLMUsage(
            prompt_tokens=(
                usage_data.get("input_tokens", 0)
                + cache_read_tokens
                + cache_write_tokens
            ),
            completion_tokens=usage_data.get("output_tokens", 0),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

        return LLMChunk(
            message=message, usage=usage, finish_reason=data.get("stop_reason")
        )
