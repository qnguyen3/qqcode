from __future__ import annotations

import asyncio
import time

import pytest

from vibe.core.config import ProviderConfig
from vibe.core.llm.backend.anthropic_sdk import AnthropicBackend
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.oauth.token import AnthropicOAuthToken, OAuthToken

FLOW_NAME = "test-refresh-flow"


class CountingFlow:
    name = FLOW_NAME
    is_subscription = True

    def __init__(self) -> None:
        self.calls = 0
        self.requested_names: list[str] = []

    async def refresh(self, token: OAuthToken) -> AnthropicOAuthToken:
        self.calls += 1
        await asyncio.sleep(0.02)
        return AnthropicOAuthToken.from_token_response(
            access_token="new-access",
            refresh_token=token.refresh_token,
            expires_in=3600,
        )


class FailingFlow:
    name = FLOW_NAME
    is_subscription = True

    async def refresh(self, token: OAuthToken) -> AnthropicOAuthToken:
        raise ValueError("refresh failed")


def _expired_token() -> AnthropicOAuthToken:
    return AnthropicOAuthToken(
        access_token="old-access",
        refresh_token="refresh-me",
        expires_in=3600,
        expires_at=int(time.time()) - 1,
    )


def _provider(oauth_flow: str = FLOW_NAME) -> ProviderConfig:
    return ProviderConfig(
        name="anthropic",
        api_base="https://api.anthropic.com",
        api_key_env_var="TEST_ANTHROPIC_KEY",
        oauth_flow=oauth_flow,
        oauth_token=_expired_token(),
    )


class TestOAuthTokenExpiry:
    def test_10_percent_buffer_still_applies(self) -> None:
        token = OAuthToken(
            access_token="a",
            refresh_token="r",
            expires_in=600,
            expires_at=int(time.time()) + 30,
        )

        assert token.is_expired() is True

    def test_skew_expires_token_early(self) -> None:
        token = OAuthToken(
            access_token="a",
            refresh_token="r",
            expires_in=120,
            expires_at=int(time.time()) + 30,
        )

        assert token.is_expired() is False
        assert token.is_expired(skew=60) is True


class TestGenericBackendRefresh:
    @pytest.mark.asyncio
    async def test_concurrent_requests_refresh_once_and_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        backend = GenericBackend(provider=provider)
        flow = CountingFlow()
        saved: list[tuple[str, AnthropicOAuthToken]] = []
        requested: list[str] = []

        def fake_get_flow(name: str) -> CountingFlow:
            requested.append(name)
            return flow

        monkeypatch.setattr("vibe.core.llm.backend.generic.get_flow", fake_get_flow)
        monkeypatch.setattr(
            "vibe.core.llm.backend.generic.save_oauth_token",
            lambda name, token: saved.append((name, token)),
        )

        results = await asyncio.gather(
            backend._get_auth_info(), backend._get_auth_info()
        )

        assert flow.calls == 1
        assert all(api_key == "new-access" for api_key, _, _ in results)
        assert provider.oauth_token is not None
        assert provider.oauth_token.access_token == "new-access"
        assert [name for name, _ in saved] == ["anthropic"]
        assert saved[0][1].access_token == "new-access"
        assert requested == [FLOW_NAME]

    @pytest.mark.asyncio
    async def test_flow_defaults_to_provider_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider(oauth_flow="")
        backend = GenericBackend(provider=provider)
        flow = CountingFlow()
        requested: list[str] = []

        def fake_get_flow(name: str) -> CountingFlow:
            requested.append(name)
            return flow

        monkeypatch.setattr("vibe.core.llm.backend.generic.get_flow", fake_get_flow)
        monkeypatch.setattr(
            "vibe.core.llm.backend.generic.save_oauth_token", lambda name, token: None
        )

        await backend._get_auth_info()

        assert requested == ["anthropic"]

    @pytest.mark.asyncio
    async def test_fresh_token_is_not_refreshed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        provider.oauth_token = AnthropicOAuthToken.from_token_response(
            access_token="fresh", refresh_token="r", expires_in=3600
        )
        backend = GenericBackend(provider=provider)
        flow = CountingFlow()
        monkeypatch.setattr("vibe.core.llm.backend.generic.get_flow", lambda name: flow)

        api_key, _, _ = await backend._get_auth_info()

        assert flow.calls == 0
        assert api_key == "fresh"

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_back_to_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        backend = GenericBackend(provider=provider)
        monkeypatch.setenv("TEST_ANTHROPIC_KEY", "env-key")
        monkeypatch.setattr(
            "vibe.core.llm.backend.generic.get_flow", lambda name: FailingFlow()
        )

        api_key, _, _ = await backend._get_auth_info()

        assert api_key == "env-key"
        assert provider.oauth_token is not None
        assert provider.oauth_token.access_token == "old-access"


class TestAnthropicBackendRefresh:
    @pytest.mark.asyncio
    async def test_concurrent_requests_refresh_once_and_persist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        backend = AnthropicBackend(provider=provider)
        flow = CountingFlow()
        saved: list[tuple[str, AnthropicOAuthToken]] = []

        monkeypatch.setattr(
            "vibe.core.llm.backend.anthropic_sdk.get_flow", lambda name: flow
        )
        monkeypatch.setattr(
            "vibe.core.llm.backend.anthropic_sdk.save_oauth_token",
            lambda name, token: saved.append((name, token)),
        )

        await asyncio.gather(
            backend._ensure_fresh_token(), backend._ensure_fresh_token()
        )

        assert flow.calls == 1
        assert provider.oauth_token is not None
        assert provider.oauth_token.access_token == "new-access"
        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_fresh_token_is_not_refreshed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        provider.oauth_token = AnthropicOAuthToken.from_token_response(
            access_token="fresh", refresh_token="r", expires_in=3600
        )
        backend = AnthropicBackend(provider=provider)
        flow = CountingFlow()
        monkeypatch.setattr(
            "vibe.core.llm.backend.anthropic_sdk.get_flow", lambda name: flow
        )

        await backend._ensure_fresh_token()

        assert flow.calls == 0
        assert provider.oauth_token.access_token == "fresh"

    @pytest.mark.asyncio
    async def test_refresh_failure_clears_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider()
        backend = AnthropicBackend(provider=provider)
        monkeypatch.setattr(
            "vibe.core.llm.backend.anthropic_sdk.get_flow", lambda name: FailingFlow()
        )

        await backend._ensure_fresh_token()

        assert provider.oauth_token is None
