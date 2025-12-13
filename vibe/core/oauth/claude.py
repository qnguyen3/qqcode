from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

from vibe.core.oauth.token import OAuthToken

# OAuth constants from Crush CLI implementation
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"


def _base64_url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_pkce_challenge() -> tuple[str, str]:
    """Generate PKCE verifier and challenge.

    Returns:
        Tuple of (verifier, challenge) strings.
    """
    # Generate 32 random bytes for verifier
    verifier_bytes = secrets.token_bytes(32)
    verifier = _base64_url_encode(verifier_bytes)

    # Create challenge by SHA-256 hashing the verifier
    challenge_hash = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64_url_encode(challenge_hash)

    return verifier, challenge


def get_authorize_url(challenge: str, state: str) -> str:
    """Build the OAuth authorization URL.

    Args:
        challenge: The PKCE challenge string.
        state: The state parameter (typically the verifier for PKCE).

    Returns:
        The full authorization URL to open in browser.
    """
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_token(code: str, verifier: str) -> OAuthToken:
    """Exchange authorization code for access and refresh tokens.

    Args:
        code: The authorization code from the OAuth callback (may include state after #).
        verifier: The PKCE verifier used when generating the challenge.

    Returns:
        OAuthToken with access token, refresh token, and expiration info.

    Raises:
        ValueError: If the token response is invalid.
    """
    # The code may include the state after # - extract both parts
    code = code.strip()
    if "#" in code:
        parts = code.split("#", 1)
        pure_code = parts[0]
        state = parts[1] if len(parts) > 1 else ""
    else:
        pure_code = code
        state = ""

    # Crush CLI sends JSON body, not form-encoded
    data = {
        "code": pure_code,
        "state": state,
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            json=data,  # Send as JSON
            headers={
                "Content-Type": "application/json",
                "User-Agent": "anthropic",
            },
        )
        if not response.is_success:
            # Try to get error details from response
            try:
                error_data = response.json()
                error_msg = error_data.get("error_description") or error_data.get("message") or error_data.get("error") or response.text
            except Exception:
                error_msg = response.text
            raise ValueError(f"Token exchange failed: {error_msg}")

        token_data = response.json()

    return OAuthToken.from_token_response(
        access_token=token_data["access_token"],
        refresh_token=token_data["refresh_token"],
        expires_in=token_data.get("expires_in", 3600),
    )


async def refresh_token(refresh_token_str: str) -> OAuthToken:
    """Refresh an expired access token.

    Args:
        refresh_token_str: The refresh token to use.

    Returns:
        New OAuthToken with fresh access token.

    Raises:
        ValueError: If the refresh request fails.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_str,
        "client_id": CLIENT_ID,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            json=data,  # Send as JSON
            headers={
                "Content-Type": "application/json",
                "User-Agent": "anthropic",
            },
        )
        if not response.is_success:
            try:
                error_data = response.json()
                error_msg = error_data.get("error_description") or error_data.get("message") or error_data.get("error") or response.text
            except Exception:
                error_msg = response.text
            raise ValueError(f"Token refresh failed: {error_msg}")

        token_data = response.json()

    return OAuthToken.from_token_response(
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", refresh_token_str),
        expires_in=token_data.get("expires_in", 3600),
    )
