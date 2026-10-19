from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import os
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

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_MAX_RETRIES = 3
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
UNAUTHORIZED_STATUSES = {401, 403}


@dataclass(frozen=True)
class _DeviceFlow:
    device_code: str
    info: DeviceCodeInfo


def _oauth_host() -> str:
    override = os.getenv("KIMI_CODE_OAUTH_HOST") or os.getenv("KIMI_OAUTH_HOST")
    return (override or DEFAULT_OAUTH_HOST).rstrip("/")


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DeviceFlowError(f"Invalid Kimi Code response field: {key}")
    return value


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


def _token_from(data: Mapping[str, Any]) -> OAuthToken:
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise DeviceFlowError("Invalid Kimi Code token response: expires_in")
    return OAuthToken.from_token_response(
        _require_str(data, "access_token"),
        _require_str(data, "refresh_token"),
        int(expires_in),
    )


async def _start_device_authorization(host: str) -> _DeviceFlow:
    ok, status, data = await _post_form(
        f"{host}/api/oauth/device_authorization", {"client_id": CLIENT_ID}
    )
    if not ok:
        raise DeviceFlowError(
            f"Kimi Code device authorization failed ({status}): {data}"
        )
    interval = data.get("interval")
    expires_in = data.get("expires_in")
    return _DeviceFlow(
        device_code=_require_str(data, "device_code"),
        info=DeviceCodeInfo(
            user_code=_require_str(data, "user_code"),
            verification_uri=_require_str(data, "verification_uri_complete"),
            interval_seconds=(
                float(interval)
                if isinstance(interval, (int, float)) and interval > 0
                else DEFAULT_POLL_INTERVAL_SECONDS
            ),
            expires_in_seconds=(
                float(expires_in)
                if isinstance(expires_in, (int, float)) and expires_in > 0
                else DEVICE_CODE_TIMEOUT_SECONDS
            ),
        ),
    )


async def _poll_for_token(
    host: str, device: _DeviceFlow, cancel_event: asyncio.Event
) -> OAuthToken:
    async def poll() -> PollResult[OAuthToken]:
        ok, status, data = await _post_form(
            f"{host}/api/oauth/token",
            {
                "client_id": CLIENT_ID,
                "device_code": device.device_code,
                "grant_type": DEVICE_CODE_GRANT_TYPE,
            },
        )
        if ok and isinstance(data.get("access_token"), str):
            return PollResult.complete(_token_from(data))
        error = data.get("error")
        match error:
            case "authorization_pending":
                return PollResult.pending()
            case "slow_down":
                interval = data.get("interval")
                return PollResult.slow_down(
                    float(interval)
                    if isinstance(interval, (int, float)) and interval > 0
                    else None
                )
            case "expired_token":
                return PollResult.failed(
                    "Kimi Code device authorization expired. Please restart login."
                )
            case "access_denied":
                return PollResult.failed("Kimi Code login was denied.")
            case _:
                return PollResult.failed(
                    f"Kimi Code device token request failed ({status}): {error}"
                )

    return await poll_device_code_flow(
        poll=poll,
        cancel_event=cancel_event,
        interval_seconds=device.info.interval_seconds,
        expires_in_seconds=device.info.expires_in_seconds,
        wait_before_first_poll=True,
    )


def _retryable(status: int) -> bool:
    return status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR


async def _refresh(
    host: str, refresh_token: str, cancel_event: asyncio.Event
) -> OAuthToken:
    last_error: Exception | None = None
    for attempt in range(REFRESH_MAX_RETRIES + 1):
        if attempt > 0:
            await asyncio.sleep(2 ** (attempt - 1))
        if cancel_event.is_set():
            raise DeviceFlowError("Kimi Code token refresh aborted")
        try:
            ok, status, data = await _post_form(
                f"{host}/api/oauth/token",
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        if ok:
            return _token_from(data)
        if status in UNAUTHORIZED_STATUSES or data.get("error") == "invalid_grant":
            raise DeviceFlowError(f"Kimi Code token refresh unauthorized ({status})")
        if _retryable(status) and attempt < REFRESH_MAX_RETRIES:
            last_error = DeviceFlowError(f"Kimi Code token refresh failed ({status})")
            continue
        raise DeviceFlowError(f"Kimi Code token refresh failed ({status}): {data}")
    raise last_error or DeviceFlowError("Kimi Code token refresh failed")


class KimiCodingOAuthFlow:
    """Kimi Code subscription login via the device authorization grant."""

    name: ClassVar[str] = "kimi-coding"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with Kimi Code"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        host = _oauth_host()
        device = await _start_device_authorization(host)
        await interaction.show_device_code(device.info)
        return await _poll_for_token(host, device, interaction.cancel_event)

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _refresh(_oauth_host(), token.refresh_token, asyncio.Event())


register_flow(KimiCodingOAuthFlow())
