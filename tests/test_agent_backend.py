from __future__ import annotations

import httpx
import pytest
import respx

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    ThinkingLevel,
    VibeConfig,
)
from vibe.core.types import AssistantEvent


@pytest.fixture
def vibe_config() -> VibeConfig:
    return VibeConfig(session_logging=SessionLoggingConfig(enabled=False))


@pytest.mark.asyncio
async def test_passes_x_affinity_header_when_asking_an_answer(vibe_config: VibeConfig):
    backend = FakeBackend([mock_llm_chunk(content="Response", finish_reason="stop")])
    agent = Agent(vibe_config, backend=backend)

    [_ async for _ in agent.act("Hello")]

    assert len(backend.requests_extra_headers) > 0
    headers = backend.requests_extra_headers[0]
    assert headers is not None
    assert "x-affinity" in headers
    assert headers["x-affinity"] == agent.session_id


@pytest.mark.asyncio
async def test_passes_x_affinity_header_when_asking_an_answer_streaming(
    vibe_config: VibeConfig,
):
    backend = FakeBackend([mock_llm_chunk(content="Response", finish_reason="stop")])
    agent = Agent(vibe_config, backend=backend, enable_streaming=True)

    [_ async for _ in agent.act("Hello")]

    assert len(backend.requests_extra_headers) > 0
    headers = backend.requests_extra_headers[0]
    assert headers is not None
    assert "x-affinity" in headers
    assert headers["x-affinity"] == agent.session_id


@pytest.mark.asyncio
async def test_updates_tokens_stats_based_on_backend_response(vibe_config: VibeConfig):
    chunk = mock_llm_chunk(
        content="Response",
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=50,
    )
    backend = FakeBackend([chunk])
    agent = Agent(vibe_config, backend=backend)

    [_ async for _ in agent.act("Hello")]

    assert agent.stats.context_tokens == 150


@pytest.mark.asyncio
async def test_updates_tokens_stats_based_on_backend_response_streaming(
    vibe_config: VibeConfig,
):
    final_chunk = mock_llm_chunk(
        content="Complete",
        finish_reason="stop",
        prompt_tokens=200,
        completion_tokens=75,
    )
    backend = FakeBackend([final_chunk])
    agent = Agent(vibe_config, backend=backend, enable_streaming=True)

    [_ async for _ in agent.act("Hello")]

    assert agent.stats.context_tokens == 275


@pytest.mark.asyncio
async def test_passes_session_thinking_level_to_backend(vibe_config: VibeConfig):
    backend = FakeBackend([mock_llm_chunk(content="Response", finish_reason="stop")])
    agent = Agent(vibe_config, backend=backend)

    agent.set_thinking_level(ThinkingLevel.HIGH)
    [_ async for _ in agent.act("Hello")]

    agent.set_thinking_level(None)
    [_ async for _ in agent.act("Again")]

    assert backend.requests_thinking_levels == [ThinkingLevel.HIGH, None]


@pytest.mark.asyncio
async def test_agent_honors_retry_after_from_backend():
    base_url = "https://api.example.com"
    json_response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello from retry"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    config = VibeConfig(
        active_model="retry-model",
        providers=[
            ProviderConfig(name="local", api_base=f"{base_url}/v1", api_key_env_var="")
        ],
        models=[ModelConfig(name="model", provider="local", alias="retry-model")],
        session_logging=SessionLoggingConfig(enabled=False),
    )

    with respx.mock(base_url=base_url) as mock_api:
        route = mock_api.post("/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(
                    status_code=429, headers={"Retry-After": "0"}, text="slow down"
                ),
                httpx.Response(status_code=200, json=json_response),
            ]
        )
        agent = Agent(config)

        events = [event async for event in agent.act("Hello")]
        content = "".join(
            event.content for event in events if isinstance(event, AssistantEvent)
        )

        assert "Hello from retry" in content
        assert route.call_count == 2
