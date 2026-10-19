from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from vibe.core import aws
from vibe.core.llm.backend.adapters.base import (
    APIAdapter,
    PreparedRequest,
    register_adapter,
)
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
    from vibe.core.config import ProviderConfig, ThinkingLevel
    from vibe.core.llm.cache import CachePlan

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_use",
}
_TOOL_CHOICES = {"auto": {"auto": {}}, "any": {"any": {}}}


def _parse_arguments(arguments: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@register_adapter("bedrock-converse")
class BedrockConverseAdapter(APIAdapter):
    """Amazon Bedrock ``Converse`` adapter with SigV4 or bearer-token auth.

    Only the non-streaming ``Converse`` operation is implemented; callers that
    stream are served by aggregating this response (see ``non_streaming_only``).
    """

    endpoint: ClassVar[str] = "/model"
    non_streaming_only: ClassVar[bool] = True

    def build_payload(
        self,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system_prompt, conversation = self._convert_messages(messages)
        inference: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            inference["maxTokens"] = max_tokens

        payload: dict[str, Any] = {
            "messages": conversation,
            "inferenceConfig": inference,
        }
        if system_prompt:
            payload["system"] = [{"text": system_prompt}]
        if tools and (tool_config := self._convert_tools(tools, tool_choice)):
            payload["toolConfig"] = tool_config
        if extra_body:
            payload["additionalModelRequestFields"] = extra_body
        return payload

    @staticmethod
    def _convert_tools(
        tools: list[AvailableTool], tool_choice: StrToolChoice | AvailableTool | None
    ) -> dict[str, Any] | None:
        if tool_choice == "none":
            return None
        specs = [
            {
                "toolSpec": {
                    "name": tool.function.name,
                    "description": tool.function.description or "",
                    "inputSchema": {
                        "json": tool.function.parameters
                        or {"type": "object", "properties": {}}
                    },
                }
            }
            for tool in tools
        ]
        config: dict[str, Any] = {"tools": specs}
        match tool_choice:
            case "auto" | "any" as choice:
                config["toolChoice"] = _TOOL_CHOICES[choice]
            case AvailableTool():
                config["toolChoice"] = {"tool": {"name": tool_choice.function.name}}
            case _:
                pass
        return config

    def _convert_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        conversation: list[dict[str, Any]] = []

        for message in messages:
            match message.role:
                case Role.system:
                    if message.content:
                        system_parts.append(message.content)
                case Role.user:
                    content = [{"text": message.content}] if message.content else []
                    conversation.append({
                        "role": "user",
                        "content": content or [{"text": ""}],
                    })
                case Role.assistant:
                    blocks: list[dict[str, Any]] = []
                    if message.content:
                        blocks.append({"text": message.content})
                    blocks.extend(
                        {
                            "toolUse": {
                                "toolUseId": call.id or "",
                                "name": call.function.name or "",
                                "input": _parse_arguments(call.function.arguments),
                            }
                        }
                        for call in message.tool_calls or []
                    )
                    if blocks:
                        conversation.append({"role": "assistant", "content": blocks})
                case Role.tool:
                    self._append_tool_result(conversation, message)

        return "\n".join(system_parts).strip(), conversation

    @staticmethod
    def _append_tool_result(
        conversation: list[dict[str, Any]], message: LLMMessage
    ) -> None:
        block = {
            "toolResult": {
                "toolUseId": message.tool_call_id or "",
                "content": [{"text": message.content or ""}],
            }
        }
        if (
            conversation
            and conversation[-1]["role"] == "user"
            and any("toolResult" in b for b in conversation[-1]["content"])
        ):
            conversation[-1]["content"].append(block)
            return
        conversation.append({"role": "user", "content": [block]})

    def _sign(self, provider: ProviderConfig, url: str, body: bytes) -> dict[str, str]:
        resolved = aws.load_aws_credentials()
        if resolved is None:
            raise RuntimeError(
                "No AWS credentials found for Amazon Bedrock; configure the "
                "environment or a shared credentials profile."
            )
        credentials, _ = resolved
        region = aws.region_from_endpoint(url) or aws.resolve_region()
        return aws.sign_request(
            method="POST", url=url, body=body, credentials=credentials, region=region
        )

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
        )
        endpoint = f"{self.endpoint}/{model_name}/converse"
        body = json.dumps(payload).encode("utf-8")
        url = f"{provider.api_base}{endpoint}"

        if api_key:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        else:
            headers = self._sign(provider, url, body)
        return PreparedRequest(endpoint, headers, body)

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        output = data.get("output") or {}
        message = output.get("message") or {}
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.get("content") or []:
            if (text := block.get("text")) is not None:
                text_parts.append(text)
            if tool_use := block.get("toolUse"):
                tool_calls.append(
                    ToolCall(
                        id=tool_use.get("toolUseId"),
                        function=FunctionCall(
                            name=tool_use.get("name"),
                            arguments=json.dumps(tool_use.get("input") or {}),
                        ),
                    )
                )

        usage = data.get("usage") or {}
        return LLMChunk(
            message=LLMMessage(
                role=Role.assistant,
                content="".join(text_parts) or None,
                tool_calls=tool_calls or None,
            ),
            finish_reason=_STOP_REASONS.get(data.get("stopReason", "")),
            usage=LLMUsage(
                prompt_tokens=usage.get("inputTokens") or 0,
                completion_tokens=usage.get("outputTokens") or 0,
                cache_read_tokens=usage.get("cacheReadInputTokens") or 0,
                cache_write_tokens=usage.get("cacheWriteInputTokens") or 0,
            ),
        )
