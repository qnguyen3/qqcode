from __future__ import annotations

import json

import httpx
import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent, AgentError
from vibe.core.config import (
    CompactionConfig,
    ModelConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.context.overflow import is_context_overflow
from vibe.core.llm.exceptions import BackendError, PayloadSummary


def make_backend_error(message: str, status: int | None = 400) -> BackendError:
    return BackendError(
        provider="test",
        endpoint="/chat/completions",
        status=status,
        reason="Bad Request",
        headers={},
        body_text=json.dumps({"error": {"message": message}}),
        parsed_error=message,
        model="test-model",
        payload_summary=PayloadSummary(
            model="test-model",
            message_count=1,
            approx_chars=10,
            temperature=0.0,
            has_tools=False,
            tool_choice=None,
        ),
    )


def make_http_status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/chat")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


def make_streaming_http_status_error(status: int, body: bytes) -> httpx.HTTPStatusError:
    """An HTTP error whose response stream was never read (as in streaming)."""
    request = httpx.Request("POST", "https://example.test/chat")
    response = httpx.Response(status, request=request, stream=httpx.ByteStream(body))
    return httpx.HTTPStatusError("request failed", request=request, response=response)


def make_agent(backend: FakeBackend) -> Agent:
    model = ModelConfig(
        name="test-model",
        provider="mistral",
        alias="test-overflow",
        context_window=200_000,
    )
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        active_model="test-overflow",
        models=[model],
        compaction=CompactionConfig(reserve_tokens=0),
    )
    return Agent(config, backend=backend)


@pytest.mark.parametrize(
    "message",
    [
        "maximum context length exceeded",
        "This model's maximum context length is 8192 tokens",
        "prompt is too long",
        "input is too long",
        "context_length_exceeded",
    ],
)
def test_backend_error_matches_overflow_patterns(message: str) -> None:
    assert is_context_overflow(make_backend_error(message, status=400))


@pytest.mark.parametrize("status", [400, 413, 422])
def test_backend_error_overflow_status_codes(status: int) -> None:
    assert is_context_overflow(make_backend_error("context window exceeded", status))


def test_backend_error_requires_overflow_status() -> None:
    assert not is_context_overflow(
        make_backend_error("maximum context length exceeded", status=429)
    )


def test_backend_error_without_pattern_is_not_overflow() -> None:
    assert not is_context_overflow(make_backend_error("invalid model name", status=400))


def test_http_status_error_uses_response_body() -> None:
    assert is_context_overflow(make_http_status_error(413, "prompt is too long"))
    assert not is_context_overflow(make_http_status_error(400, "invalid schema"))


def test_streaming_http_status_error_does_not_raise() -> None:
    """An unread SSE response must not crash classification (Regression)."""
    error = make_streaming_http_status_error(
        413, b'{"error": {"message": "prompt is too long"}}'
    )

    # The body is unavailable while the stream is unread, but classifying the
    # error must not raise httpx.ResponseNotRead.
    assert is_context_overflow(error) is False
    assert not is_context_overflow(
        make_streaming_http_status_error(429, b'{"error": "rate limited"}')
    )


def test_plain_runtime_error_is_classified_by_text() -> None:
    assert is_context_overflow(RuntimeError("The input is too long for this model"))
    assert not is_context_overflow(RuntimeError("connection reset by peer"))


def test_wrapped_backend_error_is_classified() -> None:
    try:
        raise make_backend_error("maximum context length exceeded", status=400)
    except BackendError as exc:
        wrapped = RuntimeError(f"API error: {exc}")
        wrapped.__cause__ = exc

    assert is_context_overflow(wrapped)


@pytest.mark.asyncio
async def test_agent_recovers_once_from_overflow() -> None:
    backend = FakeBackend(
        [mock_llm_chunk(content="SUMMARY"), mock_llm_chunk(content="RECOVERED")],
        exceptions=[
            make_backend_error("maximum context length exceeded", status=400),
            None,
        ],
    )
    agent = make_agent(backend)

    events = [event async for event in agent.act("Hello")]

    assert any(getattr(event, "content", None) == "RECOVERED" for event in events)
    assert agent._overflow_recovered is True
    assert len(backend.requests_messages) == 2


@pytest.mark.asyncio
async def test_agent_raises_when_compaction_recovery_fails() -> None:
    backend = FakeBackend(
        exceptions=[
            make_backend_error("maximum context length exceeded", status=400),
            RuntimeError("compaction exploded"),
        ]
    )
    agent = make_agent(backend)

    with pytest.raises(AgentError, match="Context overflow recovery failed"):
        [event async for event in agent.act("Hello")]


@pytest.mark.asyncio
async def test_agent_does_not_recover_twice_per_turn() -> None:
    backend = FakeBackend(
        [mock_llm_chunk(content="SUMMARY")],
        exceptions=[
            make_backend_error("maximum context length exceeded", status=400),
            None,
            make_backend_error("maximum context length exceeded", status=400),
        ],
    )
    agent = make_agent(backend)

    with pytest.raises(RuntimeError, match="API error"):
        [event async for event in agent.act("Hello")]
