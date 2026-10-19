from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from vibe.core.oauth.base import AuthInteraction, DeviceCodeInfo, SelectOption
from vibe.core.oauth.callback_server import (
    OAuthCallbackServer,
    start_oauth_callback_server,
    wait_for_callback_or_manual_input,
)
from vibe.core.oauth.device_code import (
    DeviceFlowError,
    PollResult,
    poll_device_code_flow,
)
from vibe.core.oauth.pkce import base64_url_decode, generate_pkce, generate_state
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"
SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
JWT_SEGMENTS = 3
PENDING_STATUSES = {403, 404}
BROWSER_METHOD = "browser"
DEVICE_METHOD = "device_code"


@dataclass(frozen=True)
class _DeviceAuth:
    device_auth_id: str
    user_code: str
    interval_seconds: float


def decode_account_id(access_token: str) -> str | None:
    """Read ``chatgpt_account_id`` from an OpenAI access-token JWT."""
    parts = access_token.split(".")
    if len(parts) != JWT_SEGMENTS:
        return None
    try:
        payload = json.loads(base64_url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError):
        return None
    auth = payload.get(JWT_CLAIM_PATH) if isinstance(payload, dict) else None
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


def _token_from(data: Mapping[str, Any], operation: str) -> OAuthToken:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    if (
        not isinstance(access, str)
        or not isinstance(refresh, str)
        or not isinstance(expires_in, (int, float))
    ):
        raise DeviceFlowError(
            f"OpenAI Codex token {operation} response missing fields: {data}"
        )
    account_id = decode_account_id(access)
    if account_id is None:
        raise DeviceFlowError("Failed to extract accountId from token")
    return OAuthToken.from_token_response(
        access, refresh, int(expires_in), metadata={"account_id": account_id}
    )


async def _post_form(url: str, fields: Mapping[str, str]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(url, data=dict(fields))


async def _post_json(url: str, payload: Mapping[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(url, json=dict(payload))


async def _exchange(code: str, verifier: str, redirect_uri: str) -> OAuthToken:
    response = await _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )
    if not response.is_success:
        raise DeviceFlowError(
            f"OpenAI Codex token exchange failed ({response.status_code}): "
            f"{response.text}"
        )
    return _token_from(response.json(), "exchange")


async def _refresh(refresh_token: str) -> OAuthToken:
    response = await _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    if not response.is_success:
        raise DeviceFlowError(
            f"OpenAI Codex token refresh failed ({response.status_code}): "
            f"{response.text}"
        )
    return _token_from(response.json(), "refresh")


def _parse_authorization_input(value: str) -> tuple[str | None, str | None]:
    trimmed = value.strip()
    if not trimmed:
        return None, None
    if "://" in trimmed:
        query = parse_qs(urlparse(trimmed).query)
        return query.get("code", [None])[0], query.get("state", [None])[0]
    if "#" in trimmed:
        code, _, state = trimmed.partition("#")
        return code, state
    if "code=" in trimmed:
        query = parse_qs(trimmed)
        return query.get("code", [None])[0], query.get("state", [None])[0]
    return trimmed, None


def _authorize_url(state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "qqcode",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def _start_callback(
    state: str, cancel_event: asyncio.Event
) -> OAuthCallbackServer[str] | None:
    try:
        return await start_oauth_callback_server(
            provider_name="OpenAI",
            host=CALLBACK_HOST,
            port=CALLBACK_PORT,
            path=CALLBACK_PATH,
            state=state,
            complete=lambda code, _params: _completed(code),
            cancel_event=cancel_event,
        )
    except OSError:
        return None


async def _completed(code: str) -> str:
    return code


async def _login_browser(interaction: AuthInteraction) -> OAuthToken:
    verifier, challenge = generate_pkce()
    state = generate_state()
    callback = await _start_callback(state, interaction.cancel_event)
    try:
        await interaction.show_auth_url(
            _authorize_url(state, challenge),
            instructions="A browser window should open. Complete login to finish.",
        )
        outcome = await wait_for_callback_or_manual_input(
            interaction,
            callback,
            message=(
                "Complete login in your browser, or paste the authorization "
                "code / redirect URL here:"
            ),
            placeholder=REDIRECT_URI,
        )
        if outcome.from_callback and outcome.value is not None:
            code = outcome.value
        else:
            code, manual_state = _parse_authorization_input(outcome.input or "")
            if manual_state and manual_state != state:
                raise DeviceFlowError("State mismatch")
        if not code:
            raise DeviceFlowError("Missing authorization code")
        return await _exchange(code, verifier, REDIRECT_URI)
    finally:
        if callback is not None:
            await callback.close()


async def _start_device_auth() -> _DeviceAuth:
    response = await _post_json(DEVICE_USER_CODE_URL, {"client_id": CLIENT_ID})
    if not response.is_success:
        raise DeviceFlowError(
            f"OpenAI Codex device code request failed ({response.status_code}): "
            f"{response.text}"
        )
    data = response.json()
    raw_interval = data.get("interval")
    interval = (
        float(raw_interval.strip())
        if isinstance(raw_interval, str) and raw_interval.strip()
        else raw_interval
    )
    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code")
    if (
        not isinstance(device_auth_id, str)
        or not isinstance(user_code, str)
        or not isinstance(interval, (int, float))
    ):
        raise DeviceFlowError(f"Invalid OpenAI Codex device code response: {data}")
    return _DeviceAuth(device_auth_id, user_code, float(interval))


async def _poll_device_auth(
    device: _DeviceAuth, cancel_event: asyncio.Event
) -> tuple[str, str]:
    async def poll() -> PollResult[tuple[str, str]]:
        response = await _post_json(
            DEVICE_TOKEN_URL,
            {"device_auth_id": device.device_auth_id, "user_code": device.user_code},
        )
        if response.is_success:
            data = response.json()
            code = data.get("authorization_code")
            verifier = data.get("code_verifier")
            if isinstance(code, str) and isinstance(verifier, str):
                return PollResult.complete((code, verifier))
            return PollResult.failed(
                f"Invalid OpenAI Codex device auth response: {data}"
            )
        if response.status_code in PENDING_STATUSES:
            return PollResult.pending()
        return PollResult.failed(
            f"OpenAI Codex device auth failed ({response.status_code}): {response.text}"
        )

    return await poll_device_code_flow(
        poll=poll,
        cancel_event=cancel_event,
        interval_seconds=device.interval_seconds,
        expires_in_seconds=DEVICE_CODE_TIMEOUT_SECONDS,
    )


async def _login_device_code(interaction: AuthInteraction) -> OAuthToken:
    device = await _start_device_auth()
    await interaction.show_device_code(
        DeviceCodeInfo(
            user_code=device.user_code,
            verification_uri=DEVICE_VERIFICATION_URI,
            interval_seconds=device.interval_seconds,
            expires_in_seconds=DEVICE_CODE_TIMEOUT_SECONDS,
        )
    )
    code, verifier = await _poll_device_auth(device, interaction.cancel_event)
    return await _exchange(code, verifier, DEVICE_REDIRECT_URI)


class OpenAICodexOAuthFlow:
    """OpenAI Codex login (browser PKCE or device code)."""

    name: ClassVar[str] = "openai-codex"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with OpenAI Codex"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        method = await interaction.select(
            "Select OpenAI Codex login method:",
            [
                SelectOption(id=BROWSER_METHOD, label="Browser login (default)"),
                SelectOption(id=DEVICE_METHOD, label="Device code login (headless)"),
            ],
        )
        if method == DEVICE_METHOD:
            return await _login_device_code(interaction)
        if method == BROWSER_METHOD:
            return await _login_browser(interaction)
        raise DeviceFlowError(f"Unknown OpenAI Codex login method: {method}")

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _refresh(token.refresh_token)


register_flow(OpenAICodexOAuthFlow())
