from __future__ import annotations

import json

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.backend.adapters import get_adapter
from vibe.core.llm.backend.adapters.openai_codex_responses import (
    OpenAICodexResponsesAdapter,
)
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.oauth.token import OAuthToken
from vibe.core.types import LLMMessage, Role

BASE_URL = "https://chatgpt.com/backend-api"

CODEX_RESPONSE = {
    "id": "resp_test",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}],
    "usage": {
        "input_tokens": 5,
        "output_tokens": 1,
        "input_tokens_details": {"cached_tokens": 2},
    },
}


def _token() -> OAuthToken:
    return OAuthToken.from_token_response(
        "access", "refresh", 3600, metadata={"account_id": "acc_1"}
    )


def make_provider() -> ProviderConfig:
    return ProviderConfig(
        name="openai-codex",
        api_base=BASE_URL,
        api_key_env_var="",
        api_style="openai-codex-responses",
        oauth_token=_token(),
    )


def make_model() -> ModelConfig:
    return ModelConfig(
        name="gpt-5.1-codex",
        provider="openai-codex",
        alias="openai-codex/gpt-5.1-codex",
        api_style="openai-codex-responses",
    )


def test_codex_adapter_is_registered() -> None:
    assert isinstance(
        get_adapter("openai-codex-responses"), OpenAICodexResponsesAdapter
    )


def test_prepare_request_targets_codex_path_and_sets_store_false() -> None:
    adapter = get_adapter("openai-codex-responses")
    request = adapter.prepare_request(
        model_name="gpt-5.1-codex",
        messages=[
            LLMMessage(role=Role.system, content="Be brief"),
            LLMMessage(role=Role.user, content="hi"),
        ],
        temperature=0.2,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=True,
        provider=make_provider(),
        api_key="access",
    )

    assert request.endpoint == "/codex/responses"
    assert request.headers["chatgpt-account-id"] == "acc_1"
    assert request.headers["OpenAI-Beta"] == "responses=experimental"
    assert request.headers["Authorization"] == "Bearer access"
    payload = json.loads(request.body)
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == "Be brief"
    assert payload["input"][0]["role"] == "user"


def test_account_id_header_is_omitted_without_oauth() -> None:
    adapter = get_adapter("openai-codex-responses")
    provider = ProviderConfig(
        name="openai-codex",
        api_base=BASE_URL,
        api_key_env_var="",
        api_style="openai-codex-responses",
    )
    request = adapter.prepare_request(
        model_name="gpt-5.1-codex",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.2,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=provider,
    )

    assert "chatgpt-account-id" not in request.headers


@pytest.mark.asyncio
async def test_generic_backend_selects_codex_style_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_CODEX_API_KEY", raising=False)

    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/codex/responses").mock(
            return_value=httpx.Response(status_code=200, json=CODEX_RESPONSE)
        )
        backend = GenericBackend(provider=make_provider())

        result = await backend.complete(
            model=make_model(),
            messages=[LLMMessage(role=Role.user, content="hi")],
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )

    assert result.message.content == "Hi"
    assert result.usage is not None
    assert result.usage.cache_read_tokens == 2
    assert route.calls[0].request.headers["chatgpt-account-id"] == "acc_1"


@pytest.mark.asyncio
async def test_generic_backend_streams_codex_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_CODEX_API_KEY", raising=False)
    chunks = [
        b'data: {"type":"response.output_text.delta","delta":"He"}',
        b'data: {"type":"response.output_text.delta","delta":"llo"}',
        b'data: {"type":"response.completed","response":{"status":"completed",'
        b'"usage":{"input_tokens":4,"output_tokens":2}}}',
    ]

    with respx.mock(base_url=BASE_URL) as mock_api:
        mock_api.post("/codex/responses").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                headers={"Content-Type": "text/event-stream"},
            )
        )
        backend = GenericBackend(provider=make_provider())

        results = [
            chunk
            async for chunk in backend.complete_streaming(
                model=make_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )
        ]

    assert [chunk.message.content for chunk in results[:2]] == ["He", "llo"]
    assert results[-1].finish_reason == "completed"
