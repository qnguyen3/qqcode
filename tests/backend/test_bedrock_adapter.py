from __future__ import annotations

import json

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.backend.adapters import get_adapter
from vibe.core.llm.backend.adapters.bedrock_converse import BedrockConverseAdapter
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import (
    AvailableFunction,
    AvailableTool,
    FunctionCall,
    LLMMessage,
    Role,
    ToolCall,
)

BEDROCK_BASE = "https://bedrock-runtime.us-east-1.amazonaws.com"

CONVERSE_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "Hello"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 4, "outputTokens": 2},
}


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="amazon-bedrock",
        api_base=BEDROCK_BASE,
        api_style="bedrock-converse",
        ambient="amazon-bedrock",
    )


def _model() -> ModelConfig:
    return ModelConfig(
        name="amazon.nova-lite-v1:0", provider="amazon-bedrock", alias="bedrock/nova"
    )


def test_bedrock_adapter_is_registered() -> None:
    assert isinstance(get_adapter("bedrock-converse"), BedrockConverseAdapter)


def test_prepare_request_with_bearer_token() -> None:
    adapter = get_adapter("bedrock-converse")

    request = adapter.prepare_request(
        model_name="amazon.nova-lite-v1:0",
        messages=[
            LLMMessage(role=Role.system, content="Be brief"),
            LLMMessage(role=Role.user, content="hi"),
        ],
        temperature=0.3,
        tools=None,
        max_tokens=512,
        tool_choice=None,
        enable_streaming=False,
        provider=_provider(),
        api_key="bearer-token",
    )

    assert request.endpoint == "/model/amazon.nova-lite-v1:0/converse"
    assert request.headers["Authorization"] == "Bearer bearer-token"
    payload = json.loads(request.body)
    assert payload["system"] == [{"text": "Be brief"}]
    assert payload["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert payload["inferenceConfig"] == {"temperature": 0.3, "maxTokens": 512}


def test_prepare_request_signs_with_sigv4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    adapter = get_adapter("bedrock-converse")

    request = adapter.prepare_request(
        model_name="amazon.nova-lite-v1:0",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=_provider(),
    )

    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "x-amz-date" in request.headers
    assert request.headers["content-type"] == "application/json"


def test_prepare_request_converts_tools() -> None:
    adapter = get_adapter("bedrock-converse")
    tool = AvailableTool(
        function=AvailableFunction(
            name="lookup",
            description="Look something up",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    )

    request = adapter.prepare_request(
        model_name="amazon.nova-lite-v1:0",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.3,
        tools=[tool],
        max_tokens=None,
        tool_choice="auto",
        enable_streaming=False,
        provider=_provider(),
        api_key="bearer",
    )

    tool_config = json.loads(request.body)["toolConfig"]
    assert tool_config["tools"][0]["toolSpec"]["name"] == "lookup"
    assert tool_config["toolChoice"] == {"auto": {}}


def test_parse_response_reads_text_and_usage() -> None:
    adapter = get_adapter("bedrock-converse")

    chunk = adapter.parse_response(CONVERSE_RESPONSE)

    assert chunk.message.content == "Hello"
    assert chunk.finish_reason == "stop"
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 4
    assert chunk.usage.completion_tokens == 2


def test_parse_response_reads_tool_use() -> None:
    adapter = get_adapter("bedrock-converse")

    chunk = adapter.parse_response({
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "calling"},
                    {
                        "toolUse": {
                            "toolUseId": "tu-1",
                            "name": "lookup",
                            "input": {"q": "x"},
                        }
                    },
                ],
            }
        },
        "stopReason": "tool_use",
    })

    assert chunk.message.tool_calls is not None
    call = chunk.message.tool_calls[0]
    assert call.function.name == "lookup"
    assert json.loads(call.function.arguments or "{}") == {"q": "x"}
    assert chunk.finish_reason == "tool_use"


def test_tool_results_are_grouped_into_one_user_message() -> None:
    adapter = get_adapter("bedrock-converse")

    request = adapter.prepare_request(
        model_name="amazon.nova-lite-v1:0",
        messages=[
            LLMMessage(
                role=Role.assistant,
                tool_calls=[
                    ToolCall(
                        id="tu-1",
                        function=FunctionCall(name="lookup", arguments='{"q": "x"}'),
                    )
                ],
            ),
            LLMMessage(
                role=Role.tool, name="lookup", tool_call_id="tu-1", content="result"
            ),
        ],
        temperature=0.3,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=_provider(),
        api_key="bearer",
    )

    messages = json.loads(request.body)["messages"]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"][0]["toolUse"]["name"] == "lookup"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["toolResult"]["toolUseId"] == "tu-1"


@pytest.mark.asyncio
async def test_streaming_is_served_by_aggregating_converse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    with respx.mock(base_url=BEDROCK_BASE) as mock_api:
        route = mock_api.post("/model/amazon.nova-lite-v1:0/converse").mock(
            return_value=httpx.Response(status_code=200, json=CONVERSE_RESPONSE)
        )
        backend = GenericBackend(provider=_provider())
        chunks = [
            chunk
            async for chunk in backend.complete_streaming(
                model=_model(),
                messages=[LLMMessage(role=Role.user, content="hi")],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )
        ]

    assert route.called
    assert len(chunks) == 1
    assert chunks[0].message.content == "Hello"
