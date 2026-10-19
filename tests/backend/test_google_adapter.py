from __future__ import annotations

import json

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig, ThinkingLevel
from vibe.core.llm.backend.adapters import get_adapter
from vibe.core.llm.backend.adapters.google_generative_ai import (
    GoogleGenerativeAIAdapter,
)
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import (
    AvailableFunction,
    AvailableTool,
    FunctionCall,
    ImagePart,
    LLMMessage,
    Role,
    ToolCall,
)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "Hi"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 5,
        "candidatesTokenCount": 1,
        "thoughtsTokenCount": 2,
        "totalTokenCount": 8,
        "cachedContentTokenCount": 3,
    },
}


def make_model(name: str = "gemini-2.5-flash") -> ModelConfig:
    return ModelConfig(name=name, provider="google", alias="google/model")


def make_provider(api_style: str = "google-generative-ai") -> ProviderConfig:
    return ProviderConfig(
        name="provider_name",
        api_base=f"{BASE_URL}/v1",
        api_key_env_var="API_KEY",
        api_style=api_style,
    )


def _payload(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)


def test_google_adapter_is_registered() -> None:
    assert isinstance(get_adapter("google-generative-ai"), GoogleGenerativeAIAdapter)


def test_prepare_request_builds_generate_content_payload() -> None:
    adapter = get_adapter("google-generative-ai")
    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[
            LLMMessage(role=Role.system, content="Be brief"),
            LLMMessage(role=Role.user, content="hi"),
        ],
        temperature=0.3,
        tools=None,
        max_tokens=4096,
        tool_choice=None,
        enable_streaming=False,
        provider=make_provider(),
        api_key="key",
    )

    assert request.endpoint == "/models/gemini-2.5-flash:generateContent"
    assert request.headers["x-goog-api-key"] == "key"
    payload = json.loads(request.body)
    assert payload["systemInstruction"] == {"parts": [{"text": "Be brief"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert payload["generationConfig"]["temperature"] == 0.3
    assert payload["generationConfig"]["maxOutputTokens"] == 4096


def test_streaming_request_uses_sse_endpoint() -> None:
    adapter = get_adapter("google-generative-ai")
    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=True,
        provider=make_provider(),
    )

    assert request.endpoint == "/models/gemini-2.5-flash:streamGenerateContent?alt=sse"


def test_images_map_to_inline_data_parts() -> None:
    adapter = get_adapter("google-generative-ai")
    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[
            LLMMessage(
                role=Role.user,
                content="what is this?",
                images=[ImagePart(media_type="image/png", data="aGVsbG8=")],
            )
        ],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=make_provider(),
    )

    parts = json.loads(request.body)["contents"][0]["parts"]
    assert parts[0] == {"text": "what is this?"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}


def test_tools_and_tool_choice_are_converted() -> None:
    adapter = get_adapter("google-generative-ai")
    tool = AvailableTool(
        function=AvailableFunction(
            name="lookup",
            description="Look something up",
            parameters={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        )
    )
    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.3,
        tools=[tool],
        max_tokens=None,
        tool_choice="auto",
        enable_streaming=False,
        provider=make_provider(),
    )

    payload = json.loads(request.body)
    declaration = payload["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "lookup"
    assert "$schema" not in declaration["parametersJsonSchema"]
    assert payload["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}


def test_parse_response_reads_text_and_usage() -> None:
    adapter = get_adapter("google-generative-ai")
    chunk = adapter.parse_response(GEMINI_RESPONSE)

    assert chunk.message.content == "Hi"
    assert chunk.finish_reason == "stop"
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 2
    assert chunk.usage.completion_tokens == 3
    assert chunk.usage.cache_read_tokens == 3


def test_parse_response_reads_function_calls() -> None:
    adapter = get_adapter("google-generative-ai")
    chunk = adapter.parse_response({
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]
                },
                "finishReason": "STOP",
            }
        ]
    })

    assert chunk.message.tool_calls is not None
    call = chunk.message.tool_calls[0]
    assert call.function.name == "lookup"
    assert json.loads(call.function.arguments or "{}") == {"q": "x"}


def test_tool_results_are_sent_as_function_responses() -> None:
    adapter = get_adapter("google-generative-ai")
    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[
            LLMMessage(
                role=Role.assistant,
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=FunctionCall(name="lookup", arguments='{"q": "x"}'),
                    )
                ],
            ),
            LLMMessage(
                role=Role.tool, name="lookup", tool_call_id="call-1", content="result"
            ),
        ],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=make_provider(),
    )

    contents = json.loads(request.body)["contents"]
    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][0]["functionCall"]["name"] == "lookup"
    assert contents[1]["role"] == "user"
    response = contents[1]["parts"][0]["functionResponse"]
    assert response["name"] == "lookup"
    assert response["response"] == {"output": "result"}
    assert response["id"] == "call-1"


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("gemini-3-pro-preview", {"thinkingLevel": "HIGH", "includeThoughts": True}),
        ("gemini-2.5-pro", {"thinkingBudget": 16384, "includeThoughts": True}),
    ],
)
def test_thinking_config_matches_model_family(
    model_name: str, expected: dict[str, object]
) -> None:
    adapter = get_adapter("google-generative-ai")
    request = adapter.prepare_request(
        model_name=model_name,
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=make_provider(),
        thinking_level=ThinkingLevel.HIGH,
    )

    assert json.loads(request.body)["generationConfig"]["thinkingConfig"] == expected


@pytest.mark.asyncio
async def test_generic_backend_streams_google_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    chunks = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"He"}]}}]}',
        b'data: {"candidates":[{"content":{"parts":[{"text":"llo"}]},"finishReason":"STOP"}],'
        b'"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2}}',
    ]

    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/v1/models/gemini-2.5-flash:streamGenerateContent").mock(
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

    assert [chunk.message.content for chunk in results] == ["He", "llo"]
    assert results[-1].finish_reason == "stop"
    assert route.calls[0].request.headers["x-goog-api-key"] == "test-key"


@pytest.mark.asyncio
async def test_model_api_style_selects_google_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock_api:
        google_route = mock_api.post(
            "/v1/models/gemini-2.5-flash:generateContent"
        ).mock(return_value=httpx.Response(status_code=200, json=GEMINI_RESPONSE))
        openai_route = mock_api.post("/v1/chat/completions").mock(
            return_value=httpx.Response(status_code=200, json={})
        )
        provider = make_provider("openai")
        model = make_model()
        model.api_style = "google-generative-ai"
        backend = GenericBackend(provider=provider)

        await backend.complete(
            model=model,
            messages=[LLMMessage(role=Role.user, content="hi")],
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )

        assert google_route.called
        assert not openai_route.called
