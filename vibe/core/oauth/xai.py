from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from vibe.core.oauth.base import AuthInteraction, DeviceCodeInfo
from vibe.core.oauth.device_code import (
    DeviceFlowError,
    PollResult,
    poll_device_code_flow,
)
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
REFERRER = "qqcode"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


@dataclass(frozen=True)
class _DeviceFlow:
    device_code: str
    info: DeviceCodeInfo


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DeviceFlowError(f"Invalid xAI OAuth response field: {key}")
    return value


def _positive_number(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        raise DeviceFlowError(f"Invalid xAI OAuth response field: {key}")
    return float(value)


def _error_detail(data: Mapping[str, Any]) -> str:
    error = data.get("error")
    description = data.get("error_description")
    return ": ".join(part for part in (error, description) if isinstance(part, str))


async def _post_form(
    url: str, fields: Mapping[str, str]
) -> tuple[bool, int, dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, data=dict(fields), headers={"Accept": "application/json"}
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


async def _request_device_code() -> _DeviceFlow:
    ok, status, data = await _post_form(
        DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": REFERRER}
    )
    if not ok:
        raise DeviceFlowError(
            f"xAI device authorization failed ({status}): {_error_detail(data)}"
        )
    interval = data.get("interval")
    complete = data.get("verification_uri_complete")
    verification_uri = (
        complete
        if isinstance(complete, str) and complete
        else _require_str(data, "verification_uri")
    )
    return _DeviceFlow(
        device_code=_require_str(data, "device_code"),
        info=DeviceCodeInfo(
            user_code=_require_str(data, "user_code"),
            verification_uri=verification_uri,
            interval_seconds=(
                float(interval)
                if isinstance(interval, (int, float)) and interval > 0
                else None
            ),
            expires_in_seconds=_positive_number(data, "expires_in"),
        ),
    )


def _credentials(
    data: Mapping[str, Any], previous_refresh: str | None = None
) -> OAuthToken:
    refresh_value = data.get("refresh_token")
    if refresh_value is None and previous_refresh is not None:
        refresh = previous_refresh
    else:
        refresh = _require_str(data, "refresh_token")
    expires_in = data.get("expires_in")
    lifetime = (
        _positive_number(data, "expires_in")
        if expires_in is not None
        else DEFAULT_TOKEN_LIFETIME_SECONDS
    )
    return OAuthToken.from_token_response(
        _require_str(data, "access_token"), refresh, int(lifetime)
    )


async def _poll_for_tokens(
    device: _DeviceFlow, cancel_event: asyncio.Event
) -> OAuthToken:
    async def poll() -> PollResult[OAuthToken]:
        ok, status, data = await _post_form(
            TOKEN_URL,
            {
                "grant_type": DEVICE_CODE_GRANT_TYPE,
                "client_id": CLIENT_ID,
                "device_code": device.device_code,
            },
        )
        if ok:
            return PollResult.complete(_credentials(data))
        error = data.get("error")
        match error:
            case "authorization_pending":
                return PollResult.pending()
            case "slow_down":
                interval = data.get("interval")
                return PollResult.slow_down(
                    float(interval) if isinstance(interval, (int, float)) else None
                )
            case "access_denied" | "authorization_denied":
                return PollResult.failed("xAI device authorization was denied")
            case "expired_token":
                return PollResult.failed("xAI device code expired")
            case _:
                return PollResult.failed(
                    f"xAI device token polling failed ({status}): {_error_detail(data)}"
                )

    return await poll_device_code_flow(
        poll=poll,
        cancel_event=cancel_event,
        interval_seconds=device.info.interval_seconds,
        expires_in_seconds=device.info.expires_in_seconds,
        wait_before_first_poll=True,
    )


async def _refresh(refresh_token: str) -> OAuthToken:
    ok, status, data = await _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if not ok:
        raise DeviceFlowError(
            f"xAI token refresh failed ({status}): {_error_detail(data)}"
        )
    return _credentials(data, previous_refresh=refresh_token)


class XaiOAuthFlow:
    """xAI subscription login via the device authorization grant."""

    name: ClassVar[str] = "xai"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with SuperGrok or X Premium"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        device = await _request_device_code()
        await interaction.show_device_code(device.info)
        return await _poll_for_tokens(device, interaction.cancel_event)

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _refresh(token.refresh_token)


register_flow(XaiOAuthFlow())
