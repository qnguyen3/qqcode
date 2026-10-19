from __future__ import annotations

from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

import httpx

from vibe.core.oauth.base import AuthInteraction
from vibe.core.oauth.callback_server import (
    start_oauth_callback_server,
    wait_for_callback_or_manual_input,
)
from vibe.core.oauth.device_code import DeviceFlowError
from vibe.core.oauth.pkce import generate_pkce
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

AUTHORIZE_URL = "https://openrouter.ai/auth"
TOKEN_URL = "https://openrouter.ai/api/v1/auth/keys"
CALLBACK_HOST = "127.0.0.1"
LOGIN_TIMEOUT_SECONDS = 5 * 60
PERMANENT_EXPIRES_IN = 10 * 365 * 24 * 3600


def parse_authorization_code(value: str) -> str | None:
    """Extract an authorization code from a URL, query string, or bare code."""
    trimmed = value.strip()
    if not trimmed:
        return None
    if "://" in trimmed:
        return parse_qs(urlparse(trimmed).query).get("code", [None])[0]
    if "code=" in trimmed:
        return parse_qs(trimmed).get("code", [None])[0]
    return trimmed


async def _exchange(code: str, verifier: str) -> OAuthToken:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            json={
                "code": code,
                "code_verifier": verifier,
                "code_challenge_method": "S256",
            },
            headers={"Accept": "application/json"},
        )
    if not response.is_success:
        raise DeviceFlowError(
            f"OpenRouter OAuth key exchange failed ({response.status_code})"
        )
    data = response.json()
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        raise DeviceFlowError('OpenRouter OAuth response carries no "key"')
    return OAuthToken.from_token_response(key, "", PERMANENT_EXPIRES_IN)


class OpenRouterOAuthFlow:
    """OpenRouter PKCE login exchanging a code for a permanent API key."""

    name: ClassVar[str] = "openrouter"
    is_subscription: ClassVar[bool] = False
    login_label: ClassVar[str] = "Sign in with OpenRouter"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        verifier, challenge = generate_pkce()
        callback = await start_oauth_callback_server(
            provider_name="OpenRouter",
            host=CALLBACK_HOST,
            port=0,
            path=f"/oauth/callback/{uuid.uuid4()}",
            complete=lambda code, _params: _exchange(code, verifier),
            cancel_event=interaction.cancel_event,
            timeout_seconds=LOGIN_TIMEOUT_SECONDS,
        )
        try:
            authorize = f"{AUTHORIZE_URL}?{urlencode({'callback_url': callback.redirect_uri, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"
            await interaction.progress(
                f"Listening for OpenRouter OAuth callback on {callback.redirect_uri}"
            )
            await interaction.show_auth_url(
                authorize,
                instructions=(
                    "Complete sign-in in your browser. If the browser is on "
                    "another machine, paste the final redirect URL here."
                ),
            )
            outcome = await wait_for_callback_or_manual_input(
                interaction,
                callback,
                message=(
                    "Complete sign-in in your browser, or paste the "
                    "authorization code / redirect URL here:"
                ),
                placeholder=callback.redirect_uri,
            )
            if outcome.from_callback and outcome.value is not None:
                return outcome.value
            code = parse_authorization_code(outcome.input or "")
            if not code:
                raise DeviceFlowError("Missing authorization code")
            await interaction.progress(
                "Exchanging authorization code for an API key..."
            )
            return await _exchange(code, verifier)
        finally:
            await callback.close()

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        raise DeviceFlowError(
            "OpenRouter credentials do not expire; sign in again if rejected"
        )


register_flow(OpenRouterOAuthFlow())
