from __future__ import annotations

import json

import httpx
import pytest
import respx

from vibe.core.config import ModelConfig, ProviderConfig, ThinkingLevel
from vibe.core.llm.backend.adapters import get_adapter
from vibe.core.llm.backend.adapters.pi_messages import PiMessagesAdapter
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

BASE_URL = "https://radius.example"

MESSAGES = [LLMMessage(role=Role.user, content="hi")]


def make_model(api_style: str | None = "pi-messages") -> ModelConfig:
    return ModelConfig(
        name="claude-sonnet-4-5",
        provider="radius",
        alias="radius/claude-sonnet-4.5",
        api_style=api_style,
    )


def make_provider() -> ProviderConfig:
    return ProviderConfig(
        name="radius",
        api_base=f"{BASE_URL}/v1",
        api_key_env_var="API_KEY",
        api_style="pi-messages",
    )


def test_pi_messages_adapter_is_registered() -> None:
    assert isinstance(get_adapter("pi-messages"), PiMessagesAdapter)
    assert PiMessagesAdapter.streaming_only is True


def test_prepare_request_builds_context_and_options() -> None:
    adapter = get_adapter("pi-messages")
    tool = AvailableTool(
        function=AvailableFunction(
            name="lookup", description="Look up", parameters={"type": "object"}
        )
    )
    request = adapter.prepare_request(
        model_name="claude-sonnet-4-5",
        messages=[
            LLMMessage(role=Role.system, content="Be brief"),
            LLMMessage(
                role=Role.user,
                content="see this",
                images=[ImagePart(media_type="image/png", data="aGk=")],
            ),
        ],
        temperature=0.3,
        tools=[tool],
        max_tokens=2048,
        tool_choice="any",
        enable_streaming=True,
        provider=make_provider(),
        api_key="key",
        thinking_level=ThinkingLevel.LOW,
    )

    assert request.endpoint == "/messages"
    assert request.headers["Accept"] == "text/event-stream"
    assert request.headers["Authorization"] == "Bearer key"
    payload = json.loads(request.body)
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["options"] == {
        "temperature": 0.3,
        "maxTokens": 2048,
        "toolChoice": "required",
        "reasoning": "low",
    }
    system, user = payload["context"]["messages"]
    assert system["role"] == "system"
    assert system["content"] == "Be brief"
    assert system["toolsAdded"][0]["name"] == "lookup"
    assert user["role"] == "user"
    assert user["content"] == [
        {"type": "text", "text": "see this"},
        {"type": "image", "data": "aGk=", "mimeType": "image/png"},
    ]


def test_assistant_and_tool_messages_are_converted() -> None:
    adapter = get_adapter("pi-messages")
    request = adapter.prepare_request(
        model_name="claude-sonnet-4-5",
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
        enable_streaming=True,
        provider=make_provider(),
    )

    assistant, tool_result = json.loads(request.body)["context"]["messages"]
    assert assistant["content"][0] == {
        "type": "toolCall",
        "id": "call-1",
        "name": "lookup",
        "arguments": {"q": "x"},
    }
    assert tool_result["role"] == "toolResult"
    assert tool_result["toolCallId"] == "call-1"
    assert tool_result["content"] == [{"type": "text", "text": "result"}]


@pytest.mark.parametrize(
    ("event", "expected_content", "expected_reasoning", "expected_reason"),
    [
        ({"type": "text_delta", "delta": "hi"}, "hi", None, None),
        ({"type": "thinking_delta", "delta": "hmm"}, "", "hmm", None),
        (
            {"type": "done", "reason": "toolUse", "usage": {"input": 5, "output": 2}},
            "",
            None,
            "tool_calls",
        ),
    ],
)
def test_parse_response_maps_events(
    event: dict[str, object],
    expected_content: str | None,
    expected_reasoning: str | None,
    expected_reason: str | None,
) -> None:
    chunk = get_adapter("pi-messages").parse_response(event)

    assert chunk.message.content == expected_content
    assert chunk.message.reasoning_content == expected_reasoning
    assert chunk.finish_reason == expected_reason


@pytest.mark.asyncio
async def test_complete_aggregates_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    chunks = [
        b'data: {"type":"text_delta","contentIndex":0,"delta":"He"}',
        b'data: {"type":"text_delta","contentIndex":0,"delta":"llo"}',
        b'data: {"type":"done","reason":"stop","usage":{"input":4,"output":2}}',
    ]

    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/v1/messages").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                headers={"Content-Type": "text/event-stream"},
            )
        )
        backend = GenericBackend(provider=make_provider())

        result = await backend.complete(
            model=make_model(),
            messages=MESSAGES,
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )

    assert result.message.content == "Hello"
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 4
    assert route.calls[0].request.headers["Accept"] == "text/event-stream"


@pytest.mark.asyncio
async def test_complete_aggregates_streamed_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "test-key")
    chunks = [
        b'data: {"type":"toolcall_start","contentIndex":0,"id":"call-1",'
        b'"toolName":"lookup"}',
        b'data: {"type":"toolcall_delta","contentIndex":0,"delta":"{\\"q\\":"}',
        b'data: {"type":"toolcall_delta","contentIndex":0,"delta":"\\"x\\"}"}',
        b'data: {"type":"toolcall_end","contentIndex":0,"toolCall":{"id":"call-1",'
        b'"name":"lookup","arguments":{"q":"x"}}}',
        b'data: {"type":"done","reason":"toolUse","usage":{"input":4,"output":2}}',
    ]

    with respx.mock(base_url=BASE_URL) as mock_api:
        mock_api.post("/v1/messages").mock(
            return_value=httpx.Response(
                status_code=200,
                stream=httpx.ByteStream(stream=b"\n\n".join(chunks)),
                headers={"Content-Type": "text/event-stream"},
            )
        )
        backend = GenericBackend(provider=make_provider())

        result = await backend.complete(
            model=make_model(),
            messages=MESSAGES,
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )

    assert result.finish_reason == "tool_calls"
    assert result.message.tool_calls is not None
    assert result.message.tool_calls[0].function.name == "lookup"
