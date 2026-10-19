"""Test data for this module was generated using real LLM provider API responses,
with responses simplified and formatted to make them readable and maintainable.

To update or modify test parameters:
1. Make actual API calls to the target providers
2. Use the raw API responses as a base for updating test data
3. Simplify only where necessary for readability while preserving core structure

The closer test data remains to real API responses, the more reliable and accurate
the tests will be. Always prefer real API data over manually constructed examples.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import json

import httpx
import pytest
import respx

from tests.backend.data import Chunk, JsonResponse, ResultData, Url
from tests.backend.data.fireworks import (
    SIMPLE_CONVERSATION_PARAMS as FIREWORKS_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_SIMPLE_CONVERSATION_PARAMS as FIREWORKS_STREAMED_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_TOOL_CONVERSATION_PARAMS as FIREWORKS_STREAMED_TOOL_CONVERSATION_PARAMS,
    TOOL_CONVERSATION_PARAMS as FIREWORKS_TOOL_CONVERSATION_PARAMS,
)
from tests.backend.data.mistral import (
    SIMPLE_CONVERSATION_PARAMS as MISTRAL_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_SIMPLE_CONVERSATION_PARAMS as MISTRAL_STREAMED_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_TOOL_CONVERSATION_PARAMS as MISTRAL_STREAMED_TOOL_CONVERSATION_PARAMS,
    TOOL_CONVERSATION_PARAMS as MISTRAL_TOOL_CONVERSATION_PARAMS,
)
from tests.backend.data.zai import (
    SIMPLE_CONVERSATION_PARAMS as ZAI_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_SIMPLE_CONVERSATION_PARAMS as ZAI_STREAMED_SIMPLE_CONVERSATION_PARAMS,
    STREAMED_TOOL_CONVERSATION_PARAMS as ZAI_STREAMED_TOOL_CONVERSATION_PARAMS,
    TOOL_CONVERSATION_PARAMS as ZAI_TOOL_CONVERSATION_PARAMS,
)
from vibe.core.config import (
    Backend,
    ModelCapabilities,
    ModelConfig,
    ProviderConfig,
    ThinkingLevel,
)
from vibe.core.llm.backend.factory import BACKEND_FACTORY
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.llm.backend.mistral import MistralBackend
from vibe.core.llm.cache import build_cache_plan
from vibe.core.llm.exceptions import BackendError
from vibe.core.llm.types import BackendLike
from vibe.core.types import (
    AvailableFunction,
    AvailableTool,
    LLMChunk,
    LLMMessage,
    Role,
    ToolCall,
)
from vibe.core.utils import (
    MAX_RETRY_AFTER_SECONDS,
    _retry_after_seconds,
    get_user_agent,
)


class TestBackend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url,json_response,result_data",
        [
            *FIREWORKS_SIMPLE_CONVERSATION_PARAMS,
            *FIREWORKS_TOOL_CONVERSATION_PARAMS,
            *MISTRAL_SIMPLE_CONVERSATION_PARAMS,
            *MISTRAL_TOOL_CONVERSATION_PARAMS,
            *ZAI_SIMPLE_CONVERSATION_PARAMS,
            *ZAI_TOOL_CONVERSATION_PARAMS,
        ],
    )
    async def test_backend_complete(
        self, base_url: Url, json_response: JsonResponse, result_data: ResultData
    ):
        with respx.mock(base_url=base_url) as mock_api:
            mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )

            BackendClasses = [
                GenericBackend,
                *([MistralBackend] if base_url == "https://api.mistral.ai" else []),
            ]
            for BackendClass in BackendClasses:
                backend: BackendLike = BackendClass(provider=provider)
                model = ModelConfig(
                    name="model_name", provider="provider_name", alias="model_alias"
                )
                messages = [LLMMessage(role=Role.user, content="Just say hi")]

                result = await backend.complete(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                )

                assert result.message.content == result_data["message"]
                assert result.finish_reason == result_data["finish_reason"]
                assert result.usage is not None
                assert (
                    result.usage.prompt_tokens == result_data["usage"]["prompt_tokens"]
                )
                assert (
                    result.usage.completion_tokens
                    == result_data["usage"]["completion_tokens"]
                )

                if result.message.tool_calls is None:
                    return

                assert len(result.message.tool_calls) == len(result_data["tool_calls"])
                for i, tool_call in enumerate[ToolCall](result.message.tool_calls):
                    assert (
                        tool_call.function.name == result_data["tool_calls"][i]["name"]
                    )
                    assert (
                        tool_call.function.arguments
                        == result_data["tool_calls"][i]["arguments"]
                    )
                    assert tool_call.index == result_data["tool_calls"][i]["index"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url,chunks,result_data",
        [
            *FIREWORKS_STREAMED_SIMPLE_CONVERSATION_PARAMS,
            *FIREWORKS_STREAMED_TOOL_CONVERSATION_PARAMS,
            *MISTRAL_STREAMED_SIMPLE_CONVERSATION_PARAMS,
            *MISTRAL_STREAMED_TOOL_CONVERSATION_PARAMS,
            *ZAI_STREAMED_SIMPLE_CONVERSATION_PARAMS,
            *ZAI_STREAMED_TOOL_CONVERSATION_PARAMS,
        ],
    )
    async def test_backend_complete_streaming(
        self, base_url: Url, chunks: list[Chunk], result_data: list[ResultData]
    ):
        with respx.mock(base_url=base_url) as mock_api:
            mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(
                    status_code=200,
                    stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                    headers={"Content-Type": "text/event-stream"},
                )
            )
            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            BackendClasses = [
                GenericBackend,
                *([MistralBackend] if base_url == "https://api.mistral.ai" else []),
            ]
            for BackendClass in BackendClasses:
                backend: BackendLike = BackendClass(provider=provider)
                model = ModelConfig(
                    name="model_name", provider="provider_name", alias="model_alias"
                )

                messages = [
                    LLMMessage(role=Role.user, content="List files in current dir")
                ]

                results: list[LLMChunk] = []
                async for result in backend.complete_streaming(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    results.append(result)

                for result, expected_result in zip(results, result_data, strict=True):
                    assert result.message.content == expected_result["message"]
                    assert result.finish_reason == expected_result["finish_reason"]
                    assert result.usage is not None
                    assert (
                        result.usage.prompt_tokens
                        == expected_result["usage"]["prompt_tokens"]
                    )
                    assert (
                        result.usage.completion_tokens
                        == expected_result["usage"]["completion_tokens"]
                    )

                    # Check reasoning_content if present in expected result
                    if "reasoning_content" in expected_result:
                        assert (
                            result.message.reasoning_content
                            == expected_result["reasoning_content"]
                        )

                    if result.message.tool_calls is None:
                        continue

                    for i, tool_call in enumerate(result.message.tool_calls):
                        assert (
                            tool_call.function.name
                            == expected_result["tool_calls"][i]["name"]
                        )
                        assert (
                            tool_call.function.arguments
                            == expected_result["tool_calls"][i]["arguments"]
                        )
                        assert (
                            tool_call.index == expected_result["tool_calls"][i]["index"]
                        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url,backend_class,response",
        [
            (
                "https://api.fireworks.ai",
                GenericBackend,
                httpx.Response(status_code=500, text="Internal Server Error"),
            ),
            (
                "https://api.fireworks.ai",
                GenericBackend,
                httpx.Response(status_code=429, text="Rate Limit Exceeded"),
            ),
            (
                "https://api.mistral.ai",
                MistralBackend,
                httpx.Response(status_code=500, text="Internal Server Error"),
            ),
            (
                "https://api.mistral.ai",
                MistralBackend,
                httpx.Response(status_code=429, text="Rate Limit Exceeded"),
            ),
        ],
    )
    async def test_backend_complete_streaming_error(
        self,
        base_url: Url,
        backend_class: type[MistralBackend | GenericBackend],
        response: httpx.Response,
    ):
        with respx.mock(base_url=base_url) as mock_api:
            mock_api.post("/v1/chat/completions").mock(return_value=response)
            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            backend = backend_class(provider=provider)
            model = ModelConfig(
                name="model_name", provider="provider_name", alias="model_alias"
            )
            messages = [LLMMessage(role=Role.user, content="Just say hi")]
            with pytest.raises(BackendError) as e:
                async for _ in backend.complete_streaming(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                ):
                    pass
            assert e.value.status == response.status_code
            assert e.value.reason == response.reason_phrase
            assert e.value.parsed_error is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend_type", [Backend.MISTRAL, Backend.GENERIC])
    async def test_backend_user_agent(self, backend_type: Backend):
        user_agent = get_user_agent(backend_type)
        base_url = "https://api.example.com"
        json_response = {
            "id": "fake_id_1234",
            "created": 1234567890,
            "model": "devstral-latest",
            "usage": {
                "prompt_tokens": 100,
                "total_tokens": 300,
                "completion_tokens": 200,
            },
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "tool_calls": None,
                        "content": "Hey",
                    },
                }
            ],
        }
        with respx.mock(base_url=base_url) as mock_api:
            mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )

            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            backend = BACKEND_FACTORY[backend_type](provider=provider)
            model = ModelConfig(
                name="model_name", provider="provider_name", alias="model_alias"
            )
            messages = [LLMMessage(role=Role.user, content="Just say hi")]

            await backend.complete(
                model=model,
                messages=messages,
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers={"user-agent": user_agent},
            )

            assert mock_api.calls.last.request.headers["user-agent"] == user_agent

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend_type", [Backend.MISTRAL, Backend.GENERIC])
    async def test_backend_user_agent_when_streaming(self, backend_type: Backend):
        user_agent = get_user_agent(backend_type)

        base_url = "https://api.example.com"
        with respx.mock(base_url=base_url) as mock_api:
            chunks = [
                rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"devstral-latest","choices":[{"index":0,"delta":{"role":"assistant","content":"Hey"},"finish_reason":"stop"}]}'
            ]
            mock_response = httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                headers={"Content-Type": "text/event-stream"},
            )
            mock_api.post("/v1/chat/completions").mock(return_value=mock_response)

            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            backend = BACKEND_FACTORY[backend_type](provider=provider)
            model = ModelConfig(
                name="model_name", provider="provider_name", alias="model_alias"
            )
            messages = [LLMMessage(role=Role.user, content="Just say hi")]

            async for _ in backend.complete_streaming(
                model=model,
                messages=messages,
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers={"user-agent": user_agent},
            ):
                pass

            assert mock_api.calls.last.request.headers["user-agent"] == user_agent

    @pytest.mark.asyncio
    async def test_tools_disabled_by_capabilities_are_not_sent(self) -> None:
        base_url = "https://api.example.com"
        json_response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hey"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        tools = [
            AvailableTool(
                function=AvailableFunction(
                    name="some_tool",
                    description="A tool",
                    parameters={"type": "object", "properties": {}},
                )
            )
        ]

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            backend = GenericBackend(provider=provider)
            model = ModelConfig(
                name="model_name",
                provider="provider_name",
                alias="model_alias",
                capabilities=ModelCapabilities(tools=False),
            )

            await backend.complete(
                model=model,
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=tools,
                max_tokens=None,
                tool_choice="auto",
                extra_headers=None,
            )

            body = json.loads(route.calls.last.request.content)
            assert "tools" not in body
            assert "tool_choice" not in body

    @pytest.mark.asyncio
    async def test_tool_choice_disabled_by_capabilities_is_not_sent(self) -> None:
        base_url = "https://api.example.com"
        json_response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hey"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        tools = [
            AvailableTool(
                function=AvailableFunction(
                    name="some_tool",
                    description="A tool",
                    parameters={"type": "object", "properties": {}},
                )
            )
        ]

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            provider = ProviderConfig(
                name="provider_name",
                api_base=f"{base_url}/v1",
                api_key_env_var="API_KEY",
            )
            backend = GenericBackend(provider=provider)
            model = ModelConfig(
                name="model_name",
                provider="provider_name",
                alias="model_alias",
                capabilities=ModelCapabilities(tool_choice=False),
            )

            await backend.complete(
                model=model,
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=tools,
                max_tokens=None,
                tool_choice="auto",
                extra_headers=None,
            )

            body = json.loads(route.calls.last.request.content)
            assert "tools" in body
            assert "tool_choice" not in body

    @pytest.mark.asyncio
    async def test_cache_plan_adds_prompt_cache_key(self) -> None:
        base_url = "https://api.example.com"
        json_response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hey"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        provider = ProviderConfig(
            name="provider_name",
            api_base=f"{base_url}/v1",
            api_key_env_var="API_KEY",
            cache_retention="short",
        )
        model = ModelConfig(
            name="model_name",
            provider="provider_name",
            alias="model_alias",
            capabilities=ModelCapabilities(prompt_cache="short"),
        )

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            backend = GenericBackend(provider=provider)

            await backend.complete(
                model=model,
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
                cache_plan=build_cache_plan(provider, model, "s" * 70),
            )

            body = json.loads(route.calls.last.request.content)
            assert body["prompt_cache_key"] == "s" * 64


def _status_error(headers: dict[str, str]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


class _StreamingBody(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload


@pytest.mark.asyncio
async def test_streaming_error_reports_the_provider_body() -> None:
    """A failed stream must surface the provider error body, not drop it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            stream=_StreamingBody(b'{"error": {"message": "context length exceeded"}}'),
        )

    backend = GenericBackend(
        provider=ProviderConfig(
            name="provider_name",
            api_base="https://example.test/v1",
            api_key_env_var="API_KEY",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    model = ModelConfig(
        name="model_name", provider="provider_name", alias="model_alias"
    )

    with pytest.raises(BackendError) as excinfo:
        async for _ in backend.complete_streaming(
            model=model, messages=[LLMMessage(role=Role.user, content="hi")]
        ):
            pass

    error = excinfo.value
    assert error.status == 400
    assert error.body_text is not None
    assert error.parsed_error == "context length exceeded"


SIMPLE_JSON_RESPONSE = {
    "choices": [
        {"message": {"role": "assistant", "content": "Hey"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


class TestRetryAfterParsing:
    def test_parses_seconds(self) -> None:
        assert _retry_after_seconds(_status_error({"Retry-After": "12"})) == 12.0

    def test_parses_http_date(self) -> None:
        target = datetime.now(UTC) + timedelta(seconds=30)
        header = format_datetime(target, usegmt=True)

        value = _retry_after_seconds(_status_error({"Retry-After": header}))

        assert value is not None
        assert 20 <= value <= 35

    def test_missing_header_returns_none(self) -> None:
        assert _retry_after_seconds(_status_error({})) is None

    def test_invalid_header_returns_none(self) -> None:
        assert _retry_after_seconds(_status_error({"Retry-After": "soon"})) is None

    def test_non_http_error_returns_none(self) -> None:
        assert _retry_after_seconds(ValueError("boom")) is None

    def test_cap_is_one_minute(self) -> None:
        assert MAX_RETRY_AFTER_SECONDS == 60.0


class TestRetryAfterBehavior:
    def _provider(self, base_url: str) -> ProviderConfig:
        return ProviderConfig(
            name="provider_name", api_base=f"{base_url}/v1", api_key_env_var="API_KEY"
        )

    def _model(self) -> ModelConfig:
        return ModelConfig(
            name="model_name", provider="provider_name", alias="model_alias"
        )

    @pytest.mark.asyncio
    async def test_complete_retries_when_retry_after_is_set(self) -> None:
        base_url = "https://api.example.com"

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                side_effect=[
                    httpx.Response(
                        status_code=429, headers={"Retry-After": "0"}, text="slow"
                    ),
                    httpx.Response(status_code=200, json=SIMPLE_JSON_RESPONSE),
                ]
            )
            backend = GenericBackend(provider=self._provider(base_url))

            result = await backend.complete(
                model=self._model(),
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )

            assert result.message.content == "Hey"
            assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_complete_stops_when_retry_after_exceeds_cap(self) -> None:
        base_url = "https://api.example.com"

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(
                    status_code=429, headers={"Retry-After": "120"}, text="slow"
                )
            )
            backend = GenericBackend(provider=self._provider(base_url))

            with pytest.raises(BackendError):
                await backend.complete(
                    model=self._model(),
                    messages=[LLMMessage(role=Role.user, content="Just say hi")],
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                )

            assert route.call_count == 1

    @pytest.mark.asyncio
    async def test_streaming_retries_when_retry_after_is_set(self) -> None:
        base_url = "https://api.example.com"
        chunks = [
            b'data: {"id":"1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"Hey"},"finish_reason":"stop"}]}'
        ]

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                side_effect=[
                    httpx.Response(
                        status_code=429, headers={"Retry-After": "0"}, text="slow"
                    ),
                    httpx.Response(
                        status_code=200,
                        stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                        headers={"Content-Type": "text/event-stream"},
                    ),
                ]
            )
            backend = GenericBackend(provider=self._provider(base_url))

            results: list[LLMChunk] = []
            async for result in backend.complete_streaming(
                model=self._model(),
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                results.append(result)

            assert route.call_count == 2
            assert results[0].message.content == "Hey"

    @pytest.mark.asyncio
    async def test_retry_after_can_be_disabled(self) -> None:
        base_url = "https://api.example.com"

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(
                    status_code=429, headers={"Retry-After": "120"}, text="slow"
                )
            )
            backend = GenericBackend(
                provider=self._provider(base_url), honor_retry_after=False
            )

            with pytest.raises(BackendError):
                await backend.complete(
                    model=self._model(),
                    messages=[LLMMessage(role=Role.user, content="Just say hi")],
                    temperature=0.2,
                    tools=None,
                    max_tokens=None,
                    tool_choice=None,
                    extra_headers=None,
                )

            # Retry-After is ignored, so the normal tries=3 budget still applies.
            assert route.call_count == 3


class TestOpenAIResponsesBackend:
    def _provider(self, base_url: str) -> ProviderConfig:
        return ProviderConfig(
            name="provider_name",
            api_base=f"{base_url}/v1",
            api_style="openai-responses",
            api_key_env_var="API_KEY",
        )

    def _model(self, *, reasoning_level: ThinkingLevel | None = None) -> ModelConfig:
        return ModelConfig(
            name="model_name",
            provider="provider_name",
            alias="model_alias",
            capabilities=ModelCapabilities(reasoning=True),
            reasoning_level=reasoning_level,
        )

    @pytest.mark.asyncio
    async def test_complete_parses_responses_output(self) -> None:
        base_url = "https://api.example.com"
        json_response = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"Hanoi"}',
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 2},
            },
        }

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/responses").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            backend = GenericBackend(provider=self._provider(base_url))

            result = await backend.complete(
                model=self._model(reasoning_level=ThinkingLevel.HIGH),
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )

            body = json.loads(route.calls.last.request.content)
            assert body["input"][0]["content"][0]["type"] == "input_text"
            assert body["reasoning"] == {"effort": "high"}

            assert result.message.content == "Hello"
            assert result.message.reasoning_content == "thinking"
            assert result.message.tool_calls is not None
            assert result.message.tool_calls[0].function.name == "get_weather"
            assert result.message.tool_calls[0].function.arguments == '{"city":"Hanoi"}'
            assert result.usage is not None
            assert result.usage.prompt_tokens == 10
            assert result.usage.completion_tokens == 5
            assert result.usage.cache_read_tokens == 2

    @pytest.mark.asyncio
    async def test_streaming_parses_response_events(self) -> None:
        base_url = "https://api.example.com"
        events = [
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.reasoning_summary_text.delta", "delta": "think"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            },
        ]
        chunks = [f"data: {json.dumps(event)}".encode() for event in events]

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/responses").mock(
                return_value=httpx.Response(
                    status_code=200,
                    stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                    headers={"Content-Type": "text/event-stream"},
                )
            )
            backend = GenericBackend(provider=self._provider(base_url))

            results: list[LLMChunk] = []
            async for result in backend.complete_streaming(
                model=self._model(),
                messages=[LLMMessage(role=Role.user, content="Just say hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            ):
                results.append(result)

            assert route.call_count == 1
            assert [
                result.message.content for result in results if result.message.content
            ] == ["Hel", "lo"]
            assert [
                result.message.reasoning_content
                for result in results
                if result.message.reasoning_content
            ] == ["think"]
            assert results[-1].usage is not None
            assert results[-1].usage.completion_tokens == 4
