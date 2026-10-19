from __future__ import annotations

import json
from typing import Literal

import httpx
import pytest
import respx

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import (
    Backend,
    ModelCapabilities,
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.llm.cache import CachePlan, build_cache_plan
from vibe.core.types import LLMMessage, Role


def _provider(
    *,
    name: str = "anthropic",
    api_style: str = "anthropic",
    backend: Backend = Backend.ANTHROPIC,
    cache_retention: Literal["default", "none", "short", "long"] = "default",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_base="https://api.anthropic.com",
        api_style=api_style,
        backend=backend,
        cache_retention=cache_retention,
    )


def _model(prompt_cache: Literal["none", "short", "long"] = "short") -> ModelConfig:
    return ModelConfig(
        name="model",
        provider="anthropic",
        alias="model",
        capabilities=ModelCapabilities(prompt_cache=prompt_cache),
    )


class TestBuildCachePlan:
    def test_anthropic_default_is_enabled_without_session_key(self) -> None:
        plan = build_cache_plan(_provider(), _model(), "a" * 100)

        assert plan == CachePlan(enabled=True, ttl="5m", session_key=None)

    def test_cache_retention_none_disables_everything(self) -> None:
        plan = build_cache_plan(_provider(cache_retention="none"), _model(), "a" * 100)

        assert plan == CachePlan(enabled=False, ttl=None, session_key=None)

    def test_long_capability_upgrades_ttl_to_one_hour(self) -> None:
        plan = build_cache_plan(_provider(), _model(prompt_cache="long"), "session")

        assert plan.enabled is True
        assert plan.ttl == "1h"

    def test_explicit_long_without_long_capability_stays_short(self) -> None:
        plan = build_cache_plan(
            _provider(cache_retention="long"), _model(prompt_cache="none"), "session"
        )

        assert plan.enabled is True
        assert plan.ttl == "5m"

    def test_openai_default_is_disabled(self) -> None:
        provider = _provider(
            name="openrouter", api_style="openai", backend=Backend.GENERIC
        )

        plan = build_cache_plan(provider, _model(prompt_cache="none"), "session")

        assert plan == CachePlan(enabled=False, ttl=None, session_key=None)

    def test_model_capability_enables_markers_without_session_key(self) -> None:
        provider = _provider(
            name="openrouter", api_style="openai", backend=Backend.GENERIC
        )

        plan = build_cache_plan(provider, _model(prompt_cache="short"), "session")

        assert plan == CachePlan(enabled=True, ttl="5m", session_key=None)

    def test_qwen_default_keeps_legacy_markers_enabled(self) -> None:
        provider = _provider(name="qwen", api_style="openai", backend=Backend.GENERIC)

        plan = build_cache_plan(provider, _model(prompt_cache="short"), "session")

        assert plan == CachePlan(enabled=True, ttl="5m", session_key=None)

    def test_openai_explicit_retention_enables_session_key(self) -> None:
        provider = _provider(
            name="openrouter",
            api_style="openai",
            backend=Backend.GENERIC,
            cache_retention="short",
        )

        plan = build_cache_plan(provider, _model(), "s" * 100)

        assert plan.enabled is True
        assert plan.session_key is not None
        assert len(plan.session_key) == 64
        assert plan.session_key == "s" * 64

    def test_openai_long_retention_and_capability_sets_one_hour(self) -> None:
        provider = _provider(
            name="openrouter",
            api_style="openai",
            backend=Backend.GENERIC,
            cache_retention="long",
        )

        plan = build_cache_plan(provider, _model(prompt_cache="long"), "session")

        assert plan.ttl == "1h"


class TestSummarizerCacheDisabled:
    @pytest.mark.asyncio
    async def test_compact_disables_cache(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "mock")
        backend = FakeBackend([
            mock_llm_chunk(content="Response", finish_reason="stop"),
            mock_llm_chunk(content="<summary>", finish_reason="stop"),
        ])
        config = VibeConfig(
            session_logging=SessionLoggingConfig(enabled=False),
            system_prompt_id="tests",
            include_project_context=False,
            include_prompt_detail=False,
            active_model="claude",
            models=[
                ModelConfig(
                    name="claude",
                    provider="anthropic",
                    alias="claude",
                    capabilities=ModelCapabilities(prompt_cache="short"),
                )
            ],
            providers=[
                ProviderConfig(
                    name="anthropic",
                    api_base="https://api.anthropic.com",
                    api_key_env_var="ANTHROPIC_API_KEY",
                    api_style="anthropic",
                    backend=Backend.ANTHROPIC,
                )
            ],
        )
        agent = Agent(config, backend=backend)

        async for _ in agent.act("Hello"):
            pass
        await agent.compact()

        plans = backend.requests_cache_plans
        assert plans[0] is not None
        assert plans[0].enabled is True
        assert plans[-1] == CachePlan(enabled=False, ttl=None, session_key=None)


class TestQwenLegacyMarkers:
    @pytest.mark.asyncio
    async def test_qwen_keeps_cache_control_without_prompt_cache_key(self) -> None:
        base_url = "https://dashscope.aliyuncs.com"
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
            name="qwen", api_base=f"{base_url}/v1", api_key_env_var="API_KEY"
        )
        model = ModelConfig(
            name="qwen3-coder-plus",
            provider="qwen",
            alias="qwen-coder-plus",
            capabilities=ModelCapabilities(prompt_cache="short"),
        )

        with respx.mock(base_url=base_url) as mock_api:
            route = mock_api.post("/v1/chat/completions").mock(
                return_value=httpx.Response(status_code=200, json=json_response)
            )
            backend = GenericBackend(provider=provider)

            await backend.complete(
                model=model,
                messages=[
                    LLMMessage(role=Role.system, content="Be helpful"),
                    LLMMessage(role=Role.user, content="Just say hi"),
                ],
                temperature=0.2,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
                cache_plan=build_cache_plan(provider, model, "session"),
            )

            body = json.loads(route.calls.last.request.content)
            system_content = body["messages"][0]["content"]
            assert isinstance(system_content, list)
            assert system_content[0]["cache_control"] == {"type": "ephemeral"}
            assert "prompt_cache_key" not in body
