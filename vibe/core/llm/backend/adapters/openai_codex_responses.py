from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from vibe.core.config import ThinkingLevel
from vibe.core.llm.backend.adapters.base import PreparedRequest, register_adapter
from vibe.core.llm.backend.adapters.openai_responses import OpenAIResponsesAdapter
from vibe.core.llm.cache import CachePlan
from vibe.core.types import AvailableTool, StrToolChoice

if TYPE_CHECKING:
    from vibe.core.config import ProviderConfig
    from vibe.core.types import LLMMessage

_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


@register_adapter("openai-codex-responses")
class OpenAICodexResponsesAdapter(OpenAIResponsesAdapter):
    """Responses adapter for the Codex subscription backend.

    Shares the Responses request/response shape but targets the Codex path,
    always sends ``store: false`` (the backend rejects stored responses) and
    authenticates with the ChatGPT account id from the OAuth token metadata.
    """

    endpoint: ClassVar[str] = "/codex/responses"

    @staticmethod
    def _account_id(provider: ProviderConfig) -> str | None:
        token = provider.oauth_token
        if token is None:
            return None
        account_id = token.metadata.get("account_id")
        return account_id if isinstance(account_id, str) and account_id else None

    def build_headers(
        self,
        api_key: str | None = None,
        *,
        streaming: bool = False,
        account_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": "qqcode",
        }
        if streaming:
            headers["Accept"] = "text/event-stream"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

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
        payload = super().build_payload(
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
        payload["store"] = False
        payload.setdefault("instructions", _DEFAULT_INSTRUCTIONS)
        payload["text"] = {"verbosity": "low"}
        payload["include"] = ["reasoning.encrypted_content"]
        payload["parallel_tool_calls"] = True
        if tools and payload.get("tool_choice") is None:
            payload["tool_choice"] = "auto"
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
        payload["stream"] = enable_streaming

        headers = self.build_headers(
            api_key, streaming=enable_streaming, account_id=self._account_id(provider)
        )
        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(self.endpoint, headers, body)
