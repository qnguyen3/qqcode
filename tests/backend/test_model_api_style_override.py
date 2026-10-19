from __future__ import annotations

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import LLMMessage, Role

BASE_URL = "https://api.style-override.example"

OPENAI_RESPONSE = {
    "id": "chatcmpl_test",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "model_name",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}

ANTHROPIC_RESPONSE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "model_name",
    "content": [{"type": "text", "text": "Hi"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 1},
}

MESSAGES = [LLMMessage(role=Role.user, content="hi")]


def make_model(api_style: str | None) -> ModelConfig:
    return ModelConfig(
        name="model_name",
        provider="provider_name",
        alias="model_alias",
        api_style=api_style,
    )


def make_provider(api_style: str = "openai") -> ProviderConfig:
    return ProviderConfig(
        name="provider_name",
        api_base=f"{BASE_URL}/v1",
        api_key_env_var="API_KEY",
        api_style=api_style,
    )


async def _complete(model: ModelConfig, provider: ProviderConfig) -> None:
    backend = GenericBackend(provider=provider)
    await backend.complete(
        model=model,
        messages=MESSAGES,
        temperature=0.2,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        extra_headers=None,
    )


@pytest.mark.asyncio
async def test_model_api_style_overrides_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")

    with respx.mock(base_url=BASE_URL) as mock_api:
        openai_route = mock_api.post("/v1/chat/completions").mock(
            return_value=httpx.Response(status_code=200, json=OPENAI_RESPONSE)
        )
        anthropic_route = mock_api.post("/v1/messages").mock(
            return_value=httpx.Response(status_code=200, json=ANTHROPIC_RESPONSE)
        )
        provider = make_provider("openai")

        await _complete(make_model("anthropic"), provider)

        assert anthropic_route.called
        assert not openai_route.called

        await _complete(make_model(None), provider)

        assert openai_route.called


@pytest.mark.asyncio
async def test_legacy_model_without_api_style_uses_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")

    with respx.mock(base_url=BASE_URL) as mock_api:
        openai_route = mock_api.post("/v1/chat/completions").mock(
            return_value=httpx.Response(status_code=200, json=OPENAI_RESPONSE)
        )
        provider = ProviderConfig.model_validate({
            "name": "provider_name",
            "api_base": f"{BASE_URL}/v1",
            "api_key_env_var": "API_KEY",
        })
        model = ModelConfig.model_validate({
            "name": "model_name",
            "provider": "provider_name",
            "alias": "model_alias",
        })

        assert provider.api_style == "openai"
        assert model.api_style is None

        await _complete(model, provider)

        assert openai_route.called
