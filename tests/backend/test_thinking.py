from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from vibe.core.config import (
    DEFAULT_BUDGETS,
    ModelCapabilities,
    ModelConfig,
    ProviderConfig,
    ThinkingLevel,
)
from vibe.core.llm.backend.adapters.anthropic_messages import AnthropicAdapter
from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter
from vibe.core.llm.backend.anthropic_sdk import AnthropicBackend
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall

MESSAGES = [LLMMessage(role=Role.user, content="hi")]


def _openai_payload(
    *,
    thinking_level: ThinkingLevel | None = None,
    extra_body: dict[str, Any] | None = None,
    temperature: float = 0.4,
) -> dict[str, Any]:
    request = OpenAIAdapter().prepare_request(
        model_name="model",
        messages=MESSAGES,
        temperature=temperature,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=ProviderConfig(name="provider", api_base="https://example.com"),
        api_key="key",
        extra_body=extra_body,
        thinking_level=thinking_level,
    )
    return json.loads(request.body)


def _anthropic_payload(
    *,
    thinking_level: ThinkingLevel | None = None,
    extra_body: dict[str, Any] | None = None,
    temperature: float = 0.4,
) -> dict[str, Any]:
    request = AnthropicAdapter().prepare_request(
        model_name="model",
        messages=MESSAGES,
        temperature=temperature,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=ProviderConfig(name="provider", api_base="https://example.com"),
        api_key="key",
        extra_body=extra_body,
        thinking_level=thinking_level,
    )
    return json.loads(request.body)


class TestAnthropicThinkingPayload:
    def test_maps_level_to_budget_and_drops_temperature(self) -> None:
        payload = _anthropic_payload(thinking_level=ThinkingLevel.HIGH)

        assert payload["thinking"] == {
            "type": "enabled",
            "budget_tokens": DEFAULT_BUDGETS[ThinkingLevel.HIGH],
        }
        assert "temperature" not in payload

    def test_off_sends_no_thinking_and_keeps_temperature(self) -> None:
        payload = _anthropic_payload(thinking_level=ThinkingLevel.OFF)

        assert "thinking" not in payload
        assert payload["temperature"] == 0.4

    def test_none_sends_no_thinking(self) -> None:
        payload = _anthropic_payload(thinking_level=None)

        assert "thinking" not in payload

    def test_user_extra_body_wins_over_level(self) -> None:
        user_thinking = {"type": "enabled", "budget_tokens": 2000}
        payload = _anthropic_payload(
            thinking_level=ThinkingLevel.HIGH, extra_body={"thinking": user_thinking}
        )

        assert payload["thinking"] == user_thinking
        assert "temperature" not in payload


class TestOpenAIChatThinkingPayload:
    def test_maps_level_to_reasoning_effort(self) -> None:
        payload = _openai_payload(thinking_level=ThinkingLevel.MEDIUM)

        assert payload["reasoning_effort"] == "medium"
        assert payload["temperature"] == 0.4

    def test_off_sends_no_reasoning_effort(self) -> None:
        payload = _openai_payload(thinking_level=ThinkingLevel.OFF)

        assert "reasoning_effort" not in payload

    def test_none_sends_no_reasoning_effort(self) -> None:
        payload = _openai_payload(thinking_level=None)

        assert "reasoning_effort" not in payload

    def test_user_extra_body_reasoning_wins(self) -> None:
        user_reasoning = {"effort": "low"}
        payload = _openai_payload(
            thinking_level=ThinkingLevel.HIGH, extra_body={"reasoning": user_reasoning}
        )

        assert "reasoning_effort" not in payload
        assert payload["reasoning"] == user_reasoning


RESPONSE_JSON = {
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def _model(
    *, reasoning: bool = True, reasoning_level: ThinkingLevel | None = None
) -> ModelConfig:
    return ModelConfig(
        name="model",
        provider="provider",
        alias="alias",
        capabilities=ModelCapabilities(reasoning=reasoning),
        reasoning_level=reasoning_level,
    )


class TestGenericBackendThinking:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model,override,expected",
        [
            (_model(reasoning_level=ThinkingLevel.MEDIUM), None, "medium"),
            (_model(reasoning_level=ThinkingLevel.LOW), ThinkingLevel.HIGH, "high"),
            (_model(reasoning_level=ThinkingLevel.HIGH), ThinkingLevel.OFF, None),
            (_model(reasoning=False, reasoning_level=ThinkingLevel.HIGH), None, None),
            (_model(reasoning=False), ThinkingLevel.HIGH, None),
        ],
    )
    async def test_effective_level_is_sent(
        self, model: ModelConfig, override: ThinkingLevel | None, expected: str | None
    ) -> None:
        provider = ProviderConfig(
            name="provider", api_base="https://api.example.com/v1"
        )

        with respx.mock(base_url="https://api.example.com") as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=RESPONSE_JSON)
            )
            backend = GenericBackend(provider=provider)

            await backend.complete(
                model=model,
                messages=MESSAGES,
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
                thinking_level=override,
            )

            body = json.loads(route.calls.last.request.content)
            assert body.get("reasoning_effort") == expected


class TestAnthropicSdkThinking:
    def _backend(self, *, oauth: bool = False) -> AnthropicBackend:
        provider = ProviderConfig(
            name="anthropic", api_base="https://api.anthropic.com"
        )
        if oauth:
            from vibe.core.oauth.token import AnthropicOAuthToken

            provider.oauth_token = AnthropicOAuthToken.from_token_response(
                access_token="token", refresh_token="refresh", expires_in=3600
            )
        return AnthropicBackend(provider=provider)

    def _apply(
        self,
        model: ModelConfig,
        messages: list[LLMMessage],
        thinking_level: ThinkingLevel | None,
        *,
        oauth: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        backend = self._backend(oauth=oauth)
        kwargs: dict[str, Any] = {"temperature": 0.7}
        headers = backend._apply_thinking(kwargs, messages, model, thinking_level)
        return kwargs, headers

    def test_maps_level_to_budget_and_drops_temperature(self) -> None:
        kwargs, headers = self._apply(
            _model(reasoning_level=ThinkingLevel.HIGH), MESSAGES, None
        )

        assert kwargs["thinking"] == {
            "type": "enabled",
            "budget_tokens": DEFAULT_BUDGETS[ThinkingLevel.HIGH],
        }
        assert "temperature" not in kwargs
        assert "interleaved-thinking-2025-05-14" in headers["anthropic-beta"]

    def test_session_override_wins_over_model_level(self) -> None:
        kwargs, _ = self._apply(
            _model(reasoning_level=ThinkingLevel.LOW), MESSAGES, ThinkingLevel.MINIMAL
        )

        assert (
            kwargs["thinking"]["budget_tokens"]
            == DEFAULT_BUDGETS[ThinkingLevel.MINIMAL]
        )

    def test_off_sends_no_thinking_and_keeps_temperature(self) -> None:
        kwargs, headers = self._apply(
            _model(reasoning_level=ThinkingLevel.HIGH), MESSAGES, ThinkingLevel.OFF
        )

        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.7
        assert headers == {}

    def test_user_extra_body_wins(self) -> None:
        model = _model()
        model.extra_body = {"thinking": {"type": "enabled", "budget_tokens": 2000}}

        kwargs, _ = self._apply(model, MESSAGES, ThinkingLevel.HIGH)

        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2000}

    def test_incompatible_history_disables_thinking(self) -> None:
        messages = [
            LLMMessage(role=Role.user, content="hi"),
            LLMMessage(
                role=Role.assistant,
                tool_calls=[
                    ToolCall(
                        id="call_1", function=FunctionCall(name="tool", arguments="{}")
                    )
                ],
            ),
        ]

        kwargs, headers = self._apply(
            _model(reasoning_level=ThinkingLevel.HIGH), messages, None
        )

        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.7
        assert headers == {}

    def test_oauth_gets_combined_beta_header(self) -> None:
        _, headers = self._apply(
            _model(reasoning_level=ThinkingLevel.LOW), MESSAGES, None, oauth=True
        )

        assert headers["anthropic-beta"] == (
            "oauth-2025-04-20,interleaved-thinking-2025-05-14"
        )
