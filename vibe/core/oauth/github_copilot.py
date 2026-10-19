from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import time
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from vibe.core.oauth.base import AuthInteraction, DeviceCodeInfo
from vibe.core.oauth.device_code import (
    DeviceFlowCancelled,
    DeviceFlowError,
    PollResult,
    poll_device_code_flow,
)
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

CLIENT_ID = "Iv1.b507a08c87ecfe98"
SCOPE = "read:user"
GITHUB_USER_AGENT = "GitHubCopilotChat/0.35.0"
COPILOT_HEADERS = {
    "User-Agent": GITHUB_USER_AGENT,
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}
DEFAULT_DOMAIN = "github.com"


@dataclass(frozen=True)
class _DeviceFlow:
    device_code: str
    info: DeviceCodeInfo


def normalize_domain(value: str) -> str | None:
    """Return the hostname of a GitHub Enterprise URL/domain, if valid."""
    trimmed = value.strip()
    if not trimmed:
        return None
    candidate = trimmed if "://" in trimmed else f"https://{trimmed}"
    return urlsplit(candidate).hostname or None


def _device_code_url(domain: str) -> str:
    return f"https://{domain}/login/device/code"


def _access_token_url(domain: str) -> str:
    return f"https://{domain}/login/oauth/access_token"


def _copilot_token_url(domain: str) -> str:
    return f"https://api.{domain}/copilot_internal/v2/token"


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DeviceFlowError(f"Invalid GitHub Copilot response field: {key}")
    return value


async def _request_device_code(domain: str) -> _DeviceFlow:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _device_code_url(domain),
            data={"client_id": CLIENT_ID, "scope": SCOPE},
            headers={"Accept": "application/json", "User-Agent": GITHUB_USER_AGENT},
        )
    if not response.is_success:
        raise DeviceFlowError(
            f"GitHub device authorization failed ({response.status_code}): {response.text}"
        )
    data: dict[str, Any] = response.json()
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, int):
        raise DeviceFlowError("Invalid GitHub device code response: expires_in")
    interval = data.get("interval")
    return _DeviceFlow(
        device_code=_require_str(data, "device_code"),
        info=DeviceCodeInfo(
            user_code=_require_str(data, "user_code"),
            verification_uri=_require_str(data, "verification_uri"),
            interval_seconds=interval if isinstance(interval, (int, float)) else None,
            expires_in_seconds=expires_in,
        ),
    )


async def _poll_for_github_token(
    domain: str, device: _DeviceFlow, cancel_event: asyncio.Event
) -> str:
    async def poll() -> PollResult[str]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _access_token_url(domain),
                data={
                    "client_id": CLIENT_ID,
                    "device_code": device.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json", "User-Agent": GITHUB_USER_AGENT},
            )
        if not response.is_success:
            return PollResult.failed(
                f"GitHub device token request failed ({response.status_code})"
            )
        data: dict[str, Any] = response.json()
        if isinstance(data.get("access_token"), str):
            return PollResult.complete(data["access_token"])
        error = data.get("error")
        if error == "authorization_pending":
            return PollResult.pending()
        if error == "slow_down":
            interval = data.get("interval")
            return PollResult.slow_down(
                interval if isinstance(interval, (int, float)) else None
            )
        return PollResult.failed(f"Device flow failed: {error}")

    return await poll_device_code_flow(
        poll=poll,
        cancel_event=cancel_event,
        interval_seconds=device.info.interval_seconds,
        expires_in_seconds=device.info.expires_in_seconds,
        wait_before_first_poll=True,
    )


async def _exchange_copilot_token(
    domain: str, github_token: str, enterprise: str | None
) -> OAuthToken:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _copilot_token_url(domain),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {github_token}",
                **COPILOT_HEADERS,
            },
        )
    if not response.is_success:
        raise DeviceFlowError(
            f"GitHub Copilot token request failed ({response.status_code}): "
            f"{response.text}"
        )
    data: dict[str, Any] = response.json()
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise DeviceFlowError("Invalid GitHub Copilot token response: expires_at")
    metadata = {"enterprise_url": enterprise} if enterprise else {}
    return OAuthToken.from_token_response(
        access_token=_require_str(data, "token"),
        refresh_token=github_token,
        expires_in=max(int(expires_at - time.time()), 60),
        metadata=metadata,
    )


class GitHubCopilotOAuthFlow:
    """GitHub Copilot subscription login (device code + Copilot token exchange)."""

    name: ClassVar[str] = "github-copilot"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with GitHub Copilot"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        raw = await interaction.prompt_text(
            "GitHub Enterprise URL/domain (blank for github.com)",
            placeholder="company.ghe.com",
        )
        if interaction.cancelled:
            raise DeviceFlowCancelled("Login cancelled")
        enterprise = normalize_domain(raw)
        if raw.strip() and enterprise is None:
            raise ValueError("Invalid GitHub Enterprise URL/domain")
        domain = enterprise or DEFAULT_DOMAIN
        device = await _request_device_code(domain)
        await interaction.show_device_code(device.info)
        github_token = await _poll_for_github_token(
            domain, device, interaction.cancel_event
        )
        await interaction.progress("Exchanging for a GitHub Copilot token...")
        return await _exchange_copilot_token(domain, github_token, enterprise)

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _exchange_copilot_token(DEFAULT_DOMAIN, token.refresh_token, None)


register_flow(GitHubCopilotOAuthFlow())
