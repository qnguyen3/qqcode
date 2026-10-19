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
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
)

if TYPE_CHECKING:
    from vibe.core.config import ProviderConfig

_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")
_MIN_MESSAGES_FOR_HISTORY_CACHE = 2


@register_adapter("openai")
class OpenAIAdapter(APIAdapter):
    endpoint: ClassVar[str] = "/chat/completions"

    def add_cache_control_to_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Add cache_control markers for providers that support explicit caching (e.g., Qwen).

        Converts system messages to content block array format and adds cache_control
        to enable guaranteed 100% cache hit rate.
        """
        if not messages:
            return messages

        result = []
        for msg in messages:
            msg = dict(msg)  # Copy to avoid mutation
            role = msg.get("role", "")
            content = msg.get("content")

            if role == "system" and isinstance(content, str):
                # Convert system message to content block array with cache_control
                msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            result.append(msg)

        # Add cache_control to second-to-last message for conversation history caching
        if len(result) >= _MIN_MESSAGES_FOR_HISTORY_CACHE:
            target_idx = len(result) - _MIN_MESSAGES_FOR_HISTORY_CACHE
            target_msg = result[target_idx]
            content = target_msg.get("content")

            if isinstance(content, str):
                # Convert to content block array with cache_control
                result[target_idx] = {
                    **target_msg,
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            elif isinstance(content, list) and len(content) > 0:
                # Add cache_control to last block if it doesn't have one
                content = list(content)
                last_block = dict(content[-1])
                if "cache_control" not in last_block:
                    last_block["cache_control"] = {"type": "ephemeral"}
                    content[-1] = last_block
                    result[target_idx] = {**target_msg, "content": content}

        return result

    def build_payload(
        self,
        model_name: str,
        converted_messages: list[dict[str, Any]],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
        thinking_level: ThinkingLevel | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": converted_messages,
            "temperature": temperature,
        }

        if tools:
            payload["tools"] = [tool.model_dump(exclude_none=True) for tool in tools]
        if tool_choice:
            payload["tool_choice"] = (
                tool_choice
                if isinstance(tool_choice, str)
                else tool_choice.model_dump()
            )
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        if (
            effort := effective_reasoning_effort(thinking_level, reasoning_effort)
        ) is not None and not extra_body_has_reasoning(extra_body):
            payload["reasoning_effort"] = effort

        return payload

    def build_headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _message_to_payload(message: LLMMessage) -> dict[str, Any]:
        """Convert a message, folding attached images into content blocks."""
        data = message.model_dump(exclude_none=True)
        data.pop("images", None)
        if not message.images:
            return data

        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        parts.extend(
            {"type": "image_url", "image_url": {"url": image.data_uri()}}
            for image in message.images
            if image.is_loaded
        )
        data["content"] = parts
        return data

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
        converted_messages = [self._message_to_payload(msg) for msg in messages]

        # Enable explicit cache control for providers that support it (e.g., Qwen)
        if provider.name == "qwen" and not (cache_plan and not cache_plan.enabled):
            converted_messages = self.add_cache_control_to_messages(converted_messages)

        payload = self.build_payload(
            model_name,
            converted_messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            extra_body,
            thinking_level,
            reasoning_effort,
        )

        if cache_plan and cache_plan.enabled:
            extra = extra_body or {}
            if "prompt_cache_key" not in extra and cache_plan.session_key:
                payload["prompt_cache_key"] = cache_plan.session_key
            if "prompt_cache_retention" not in extra and cache_plan.ttl == "1h":
                payload["prompt_cache_retention"] = "24h"

        if enable_streaming:
            payload["stream"] = True
            if provider.name == "mistral":
                payload["stream_options"] = {"stream_tool_calls": True}

        headers = self.build_headers(api_key)

        body = json.dumps(payload).encode("utf-8")

        return PreparedRequest(self.endpoint, headers, body)

    @staticmethod
    def _extract_reasoning(message_data: dict[str, Any]) -> str | None:
        for key in _REASONING_KEYS:
            value = message_data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _parse_message(self, message_data: dict[str, Any]) -> LLMMessage:
        message = LLMMessage.model_validate(message_data)
        if message.reasoning_content is None and (
            reasoning := self._extract_reasoning(message_data)
        ):
            message = message.model_copy(update={"reasoning_content": reasoning})
        return message

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        if data.get("choices"):
            choice = data["choices"][0]
            if "message" in choice:
                message = self._parse_message(choice["message"])
            elif "delta" in choice:
                message = self._parse_message(choice["delta"])
            else:
                raise ValueError("Invalid response data")
            finish_reason = choice.get("finish_reason")

        elif "message" in data:
            message = self._parse_message(data["message"])
            finish_reason = data.get("finish_reason")
        elif "delta" in data:
            message = self._parse_message(data["delta"])
            finish_reason = None
        else:
            message = LLMMessage(role=Role.assistant, content="")
            finish_reason = None

        usage_data = data.get("usage") or {}
        prompt_details = usage_data.get("prompt_tokens_details") or {}
        cache_read_tokens = (
            prompt_details.get("cached_tokens")
            or usage_data.get("cache_read_input_tokens")
            or 0
        )
        cache_write_tokens = usage_data.get("cache_creation_input_tokens") or 0
        if (prompt_tokens := usage_data.get("prompt_tokens")) is None:
            prompt_tokens = (
                usage_data.get("input_tokens", 0)
                + cache_read_tokens
                + cache_write_tokens
            )
        usage = LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=usage_data.get("completion_tokens")
            or usage_data.get("output_tokens", 0),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

        return LLMChunk(message=message, usage=usage, finish_reason=finish_reason)
