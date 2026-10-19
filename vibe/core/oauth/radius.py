from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlencode
import uuid

import httpx

from vibe.core.oauth.base import AuthInteraction, DeviceCodeInfo, SelectOption
from vibe.core.oauth.callback_server import start_oauth_callback_server
from vibe.core.oauth.device_code import (
    DeviceFlowError,
    PollResult,
    poll_device_code_flow,
)
from vibe.core.oauth.pkce import generate_pkce
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

GATEWAY = "https://radius.pi.dev"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1456
CALLBACK_PATH = "/oauth/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
OAUTH_CLIENT_ID = "qqcode-gateway"
OAUTH_SCOPE = "gateway offline_access"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
BROWSER_METHOD = "browser"
DEVICE_METHOD = "device-code"


@dataclass(frozen=True)
class _DeviceAuthorization:
    device_code: str
    info: DeviceCodeInfo


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DeviceFlowError(f"Invalid Radius OAuth response field: {key}")
    return value


async def _post(
    path: str, fields: Mapping[str, str]
) -> tuple[bool, int, dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GATEWAY}{path}",
            data=dict(fields),
            headers={"Accept": "application/json"},
        )
    try:
        data = response.json()
    except ValueError:
        data = {}
    return (
        response.is_success,
        response.status_code,
        data if isinstance(data, dict) else {},
    )


def _token_from(data: Mapping[str, Any]) -> OAuthToken:
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise DeviceFlowError("Invalid Radius OAuth token response: expires_in")
    scope = data.get("scope")
    metadata = {"scope": scope} if isinstance(scope, str) else {}
    return OAuthToken.from_token_response(
        _require_str(data, "access_token"),
        _require_str(data, "refresh_token"),
        int(expires_in),
        metadata=metadata,
    )


async def _request_token(fields: Mapping[str, str]) -> OAuthToken:
    ok, status, data = await _post("/v1/oauth/token", fields)
    if not ok:
        raise DeviceFlowError(f"Radius OAuth token request failed ({status}): {data}")
    return _token_from(data)


async def _load_authorization_endpoint() -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GATEWAY}/v1/oauth", headers={"Accept": "application/json"}
        )
    if not response.is_success:
        raise DeviceFlowError(
            f"Could not load Radius OAuth config from {GATEWAY} "
            f"({response.status_code})"
        )
    data = response.json()
    endpoint = data.get("authorizationEndpoint") if isinstance(data, dict) else None
    if not isinstance(endpoint, str):
        raise DeviceFlowError(f"Invalid Radius OAuth config from {GATEWAY}")
    return endpoint


async def _request_device_authorization() -> _DeviceAuthorization:
    ok, status, data = await _post(
        "/v1/oauth/device", {"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE}
    )
    if not ok:
        raise DeviceFlowError(
            f"Radius OAuth device authorization failed ({status}): {data}"
        )
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise DeviceFlowError(
            "Radius OAuth device authorization response is missing required fields"
        )
    interval = data.get("interval")
    return _DeviceAuthorization(
        device_code=_require_str(data, "device_code"),
        info=DeviceCodeInfo(
            user_code=_require_str(data, "user_code"),
            verification_uri=_require_str(data, "verification_uri"),
            interval_seconds=(
                float(interval) if isinstance(interval, (int, float)) else None
            ),
            expires_in_seconds=float(expires_in),
        ),
    )


async def _login_browser(
    authorization_endpoint: str, interaction: AuthInteraction
) -> OAuthToken:
    verifier, challenge = generate_pkce()
    state = str(uuid.uuid4())
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": OAUTH_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "handoff": "url",
        "state": state,
    }
    callback = await start_oauth_callback_server(
        provider_name="Radius",
        host=CALLBACK_HOST,
        port=CALLBACK_PORT,
        path=CALLBACK_PATH,
        state=state,
        complete=lambda code, _params: _request_token({
            "grant_type": "authorization_code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        }),
        cancel_event=interaction.cancel_event,
    )
    try:
        await interaction.progress(f"Listening for OAuth callback on {REDIRECT_URI}")
        await interaction.show_auth_url(
            f"{authorization_endpoint}?{urlencode(params)}",
            instructions="Continue in your browser.",
        )
        credential = await callback.wait()
        if credential is None:
            raise DeviceFlowError("OAuth callback did not complete.")
        return credential
    finally:
        await callback.close()


async def _login_device_code(interaction: AuthInteraction) -> OAuthToken:
    device = await _request_device_authorization()
    await interaction.show_device_code(device.info)

    async def poll() -> PollResult[OAuthToken]:
        ok, status, data = await _post(
            "/v1/oauth/token",
            {
                "grant_type": DEVICE_CODE_GRANT_TYPE,
                "client_id": OAUTH_CLIENT_ID,
                "device_code": device.device_code,
            },
        )
        if ok:
            return PollResult.complete(_token_from(data))
        match data.get("error"):
            case "authorization_pending":
                return PollResult.pending()
            case "slow_down":
                return PollResult.slow_down()
            case "expired_token":
                return PollResult.failed("Device authorization expired.")
            case "access_denied":
                return PollResult.failed("Device authorization was denied.")
            case _:
                return PollResult.failed(
                    f"Radius OAuth device token request failed ({status}): {data}"
                )

    return await poll_device_code_flow(
        poll=poll,
        cancel_event=interaction.cancel_event,
        interval_seconds=device.info.interval_seconds,
        expires_in_seconds=device.info.expires_in_seconds,
    )


class RadiusOAuthFlow:
    """Radius gateway login (browser PKCE or device code)."""

    name: ClassVar[str] = "radius"
    is_subscription: ClassVar[bool] = False
    login_label: ClassVar[str] = "Sign in to Radius"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        method = await interaction.select(
            "Sign in to Radius:",
            [
                SelectOption(
                    id=BROWSER_METHOD, label="Sign in with browser (recommended)"
                ),
                SelectOption(
                    id=DEVICE_METHOD,
                    label="Sign in with device code (when signing in from another device)",
                ),
            ],
        )
        if method == DEVICE_METHOD:
            return await _login_device_code(interaction)
        if method == BROWSER_METHOD:
            endpoint = await _load_authorization_endpoint()
            return await _login_browser(endpoint, interaction)
        raise DeviceFlowError(f"Unknown Radius sign-in method: {method}")

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _request_token({
            "grant_type": "refresh_token",
            "client_id": OAUTH_CLIENT_ID,
            "refresh_token": token.refresh_token,
        })


register_flow(RadiusOAuthFlow())
