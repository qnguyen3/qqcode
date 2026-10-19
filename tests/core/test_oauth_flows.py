from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
import json
import time

import httpx
import pytest
import respx

from vibe.core.oauth.base import DeviceCodeInfo, SelectOption
from vibe.core.oauth.callback_server import start_oauth_callback_server
from vibe.core.oauth.device_code import DeviceFlowError
from vibe.core.oauth.github_copilot import GitHubCopilotOAuthFlow
from vibe.core.oauth.openai_chatgpt import (
    DIRECT_TOKEN_SCOPE,
    TOKEN_URL as CHATGPT_TOKEN_URL,
    OpenAIChatGPTOAuthFlow,
    _credentials,
)
from vibe.core.oauth.openai_codex import OpenAICodexOAuthFlow
from vibe.core.oauth.openrouter import OpenRouterOAuthFlow
from vibe.core.oauth.token import OAuthToken
from vibe.core.oauth.xai import XaiOAuthFlow

DEVICE_MIN_INTERVAL = "vibe.core.oauth.device_code.MINIMUM_INTERVAL_SECONDS"


class FakeInteraction:
    """Minimal ``AuthInteraction`` for driving flows in tests."""

    def __init__(
        self,
        *,
        texts: Sequence[str] | None = None,
        selections: Sequence[str] | None = None,
    ) -> None:
        self.cancel_event = asyncio.Event()
        self._texts = list(texts or [])
        self._selections = list(selections or [])
        self.auth_urls: list[str] = []
        self.device_codes: list[DeviceCodeInfo] = []
        self.messages: list[str] = []

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    async def prompt_secret(self, message: str) -> str:
        return await self.prompt_text(message)

    async def prompt_text(self, message: str, placeholder: str | None = None) -> str:
        if self._texts:
            return self._texts.pop(0)
        await self.cancel_event.wait()
        raise RuntimeError("cancelled")

    async def select(self, message: str, options: Sequence[SelectOption]) -> str:
        if self._selections:
            return self._selections.pop(0)
        return options[0].id

    async def show_auth_url(self, url: str, instructions: str | None = None) -> None:
        self.auth_urls.append(url)

    async def show_device_code(self, info: DeviceCodeInfo) -> None:
        self.device_codes.append(info)

    async def progress(self, message: str) -> None:
        self.messages.append(message)


def _jwt(account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id}
        }).encode()
    ).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


@pytest.mark.asyncio
async def test_github_copilot_device_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DEVICE_MIN_INTERVAL, 0.01)
    interaction = FakeInteraction(texts=[""])

    with respx.mock as mock:
        mock.post("https://github.com/login/device/code").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "device-1",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 0.01,
                    "expires_in": 900,
                },
            )
        )
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": "gho-token"})
        )
        mock.get("https://api.github.com/copilot_internal/v2/token").mock(
            return_value=httpx.Response(
                200,
                json={"token": "copilot-token", "expires_at": int(time.time()) + 3600},
            )
        )

        token = await GitHubCopilotOAuthFlow().login(interaction)

    assert token.access_token == "copilot-token"
    assert token.refresh_token == "gho-token"
    assert token.metadata == {}
    assert interaction.device_codes[0].user_code == "ABCD-1234"


@pytest.mark.asyncio
async def test_github_copilot_enterprise_domain_in_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DEVICE_MIN_INTERVAL, 0.01)
    interaction = FakeInteraction(texts=["company.ghe.com"])

    with respx.mock as mock:
        mock.post("https://company.ghe.com/login/device/code").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "device-2",
                    "user_code": "EFGH-5678",
                    "verification_uri": "https://company.ghe.com/login/device",
                    "interval": 0.01,
                    "expires_in": 900,
                },
            )
        )
        mock.post("https://company.ghe.com/login/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": "gho-enterprise"})
        )
        mock.get("https://api.company.ghe.com/copilot_internal/v2/token").mock(
            return_value=httpx.Response(
                200,
                json={"token": "copilot-ent", "expires_at": int(time.time()) + 3600},
            )
        )

        token = await GitHubCopilotOAuthFlow().login(interaction)

    assert token.metadata["enterprise_url"] == "company.ghe.com"


@pytest.mark.asyncio
async def test_xai_device_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DEVICE_MIN_INTERVAL, 0.01)
    interaction = FakeInteraction()

    with respx.mock as mock:
        mock.post("https://auth.x.ai/oauth2/device/code").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "UC",
                    "verification_uri": "https://x.ai/device",
                    "expires_in": 900,
                    "interval": 0.01,
                },
            )
        )
        mock.post("https://auth.x.ai/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "xai-access",
                    "refresh_token": "xai-refresh",
                    "expires_in": 3600,
                },
            )
        )

        token = await XaiOAuthFlow().login(interaction)

    assert token.access_token == "xai-access"
    assert token.refresh_token == "xai-refresh"
    assert interaction.device_codes[0].verification_uri == "https://x.ai/device"


@pytest.mark.asyncio
async def test_openrouter_pkce_manual_input() -> None:
    interaction = FakeInteraction(texts=["https://openrouter.ai/cb?code=or-code"])

    with respx.mock as mock:
        mock.post("https://openrouter.ai/api/v1/auth/keys").mock(
            return_value=httpx.Response(200, json={"key": "sk-or-permanent"})
        )

        token = await OpenRouterOAuthFlow().login(interaction)

    assert token.access_token == "sk-or-permanent"
    assert token.refresh_token == ""
    assert token.is_expired() is False
    assert "code_challenge=" in interaction.auth_urls[0]
    assert "callback_url=" in interaction.auth_urls[0]


@pytest.mark.asyncio
async def test_openai_codex_device_flow_extracts_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DEVICE_MIN_INTERVAL, 0.01)
    interaction = FakeInteraction(selections=["device_code"])

    with respx.mock as mock:
        mock.post("https://auth.openai.com/api/accounts/deviceauth/usercode").mock(
            return_value=httpx.Response(
                200,
                json={"device_auth_id": "dev", "user_code": "CODE", "interval": 0.01},
            )
        )
        mock.post("https://auth.openai.com/api/accounts/deviceauth/token").mock(
            return_value=httpx.Response(
                200, json={"authorization_code": "ac", "code_verifier": "cv"}
            )
        )
        mock.post("https://auth.openai.com/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": _jwt("acc_123"),
                    "refresh_token": "codex-refresh",
                    "expires_in": 3600,
                },
            )
        )

        token = await OpenAICodexOAuthFlow().login(interaction)

    assert token.access_token == _jwt("acc_123")
    assert token.metadata["account_id"] == "acc_123"


@pytest.mark.asyncio
async def test_callback_server_completes_and_checks_state() -> None:
    async def complete(code: str, params: Mapping[str, str]) -> str:
        return f"{code}:{params.get('state')}"

    cancel_event = asyncio.Event()
    server = await start_oauth_callback_server(
        provider_name="Test",
        host="127.0.0.1",
        port=0,
        path="/cb",
        state="expected",
        complete=complete,
        cancel_event=cancel_event,
    )
    try:
        async with httpx.AsyncClient() as client:
            mismatch = await client.get(f"{server.redirect_uri}?code=x&state=wrong")
            assert mismatch.status_code == 400
            ok = await client.get(f"{server.redirect_uri}?code=abc&state=expected")
            assert ok.status_code == 200
        assert await server.wait() == "abc:expected"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_callback_server_cancel_resolves_none() -> None:
    async def complete(code: str, params: Mapping[str, str]) -> str:
        return code

    cancel_event = asyncio.Event()
    server = await start_oauth_callback_server(
        provider_name="Test",
        host="127.0.0.1",
        port=0,
        path="/cb",
        complete=complete,
        cancel_event=cancel_event,
    )
    try:
        server.cancel()
        assert await server.wait() is None
    finally:
        await server.close()


def _chatgpt_token_response(client_id: str) -> dict[str, object]:
    return {
        "access_token": "chatgpt-access",
        "refresh_token": "chatgpt-refresh",
        "expires_in": 3600,
        "scope": f"openid {DIRECT_TOKEN_SCOPE}",
    }


def test_openai_chatgpt_login_persists_issued_client_id() -> None:
    token = _credentials(_chatgpt_token_response("issued-client"), "issued-client")

    assert token.metadata["client_id"] == "issued-client"
    assert token.metadata["scopes"] == ["openid", DIRECT_TOKEN_SCOPE]


@pytest.mark.asyncio
async def test_openai_chatgpt_refresh_reuses_persisted_client_id() -> None:
    token = OAuthToken.from_token_response(
        "chatgpt-access",
        "chatgpt-refresh",
        3600,
        metadata={"client_id": "issued-client"},
    )

    with respx.mock as mock:
        route = mock.post(CHATGPT_TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json=_chatgpt_token_response("issued-client")
            )
        )
        refreshed = await OpenAIChatGPTOAuthFlow().refresh(token)

    request = route.calls[0].request
    body = request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "client_id=issued-client" in body
    assert refreshed.access_token == "chatgpt-access"
    assert refreshed.metadata["client_id"] == "issued-client"


@pytest.mark.asyncio
async def test_openai_chatgpt_refresh_requires_persisted_client_id() -> None:
    token = OAuthToken.from_token_response("chatgpt-access", "chatgpt-refresh", 3600)

    with pytest.raises(DeviceFlowError, match="client ID"):
        await OpenAIChatGPTOAuthFlow().refresh(token)
