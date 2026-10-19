from __future__ import annotations

import json

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.llm.capabilities import clamp_max_tokens
from vibe.core.types import LLMMessage, Role

BASE_URL = "https://api.test-provider.example"

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


def make_model(limit: int | None) -> ModelConfig:
    return ModelConfig(
        name="model_name",
        provider="provider_name",
        alias="model_alias",
        max_output_tokens=limit,
    )


def make_provider(api_style: str) -> ProviderConfig:
    return ProviderConfig(
        name="provider_name",
        api_base=f"{BASE_URL}/v1",
        api_key_env_var="API_KEY",
        api_style=api_style,
    )


@pytest.mark.parametrize(
    ("requested", "limit", "expected"),
    [
        (4096, 8192, 4096),
        (16384, 8192, 8192),
        (8192, 8192, 8192),
        (None, 8192, None),
        (4096, None, 4096),
        (None, None, None),
    ],
)
def test_clamp_max_tokens(
    requested: int | None, limit: int | None, expected: int | None
) -> None:
    assert clamp_max_tokens(make_model(limit), requested) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "limit", "expected"),
    [(16384, 8192, 8192), (4096, 8192, 4096), (4096, None, 4096), (None, 8192, None)],
)
async def test_openai_request_sends_the_clamped_budget(
    requested: int | None, limit: int | None, expected: int | None
) -> None:
    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/v1/chat/completions").mock(
            return_value=httpx.Response(status_code=200, json=OPENAI_RESPONSE)
        )
        backend = GenericBackend(provider=make_provider("openai"))

        await backend.complete(
            model=make_model(limit),
            messages=[LLMMessage(role=Role.user, content="hi")],
            temperature=0.2,
            tools=None,
            max_tokens=requested,
            tool_choice=None,
            extra_headers=None,
        )

        payload = json.loads(route.calls[0].request.content)
        assert payload.get("max_tokens") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "limit", "expected"),
    [(16384, 8192, 8192), (4096, 8192, 4096), (None, 8192, 8192)],
)
async def test_anthropic_style_request_sends_the_clamped_budget(
    requested: int | None, limit: int | None, expected: int
) -> None:
    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/v1/messages").mock(
            return_value=httpx.Response(status_code=200, json=ANTHROPIC_RESPONSE)
        )
        backend = GenericBackend(provider=make_provider("anthropic"))

        await backend.complete(
            model=make_model(limit),
            messages=[LLMMessage(role=Role.user, content="hi")],
            temperature=0.2,
            tools=None,
            max_tokens=requested,
            tool_choice=None,
            extra_headers=None,
        )

        payload = json.loads(route.calls[0].request.content)
        assert payload["max_tokens"] == expected
