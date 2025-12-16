from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import secrets
import uuid
from urllib.parse import urlencode

import httpx

from vibe.core.oauth.token import QwenOAuthToken

# Qwen OAuth constants
CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
DEVICE_CODE_URL = "https://chat.qwen.ai/api/v1/oauth2/device/code"
TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
SCOPES = "openid profile email model.completion"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# HTTP status codes
HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY_REQUESTS = 429

# User agent for API requests (required to bypass WAF)
USER_AGENT = "qwen-code/1.0"


def _base64_url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_pkce_challenge() -> tuple[str, str]:
    """Generate PKCE verifier and challenge.

    Returns:
        Tuple of (verifier, challenge) strings.
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = _base64_url_encode(verifier_bytes)

    challenge_hash = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64_url_encode(challenge_hash)

    return verifier, challenge


@dataclass
class DeviceCodeResponse:
    """Response from the device code endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int


class QwenOAuthError(Exception):
    """Base error for Qwen OAuth operations."""

    pass


class DeviceAuthorizationError(QwenOAuthError):
    """Error during device authorization."""

    pass


class TokenPollingError(QwenOAuthError):
    """Error during token polling."""

    pass


class AuthorizationPending(TokenPollingError):
    """User has not yet approved the authorization."""

    pass


class SlowDown(TokenPollingError):
    """Server requested to slow down polling."""

    pass


class AuthorizationTimeout(TokenPollingError):
    """Authorization timed out."""

    pass


class AuthorizationCancelled(TokenPollingError):
    """Authorization was cancelled by user."""

    pass


async def request_device_code(challenge: str) -> DeviceCodeResponse:
    """Request a device code for the device authorization flow.

    Args:
        challenge: The PKCE challenge string.

    Returns:
        DeviceCodeResponse with device_code, user_code, and verification URLs.

    Raises:
        DeviceAuthorizationError: If the request fails.
    """
    data = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            DEVICE_CODE_URL,
            content=urlencode(data),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "x-request-id": str(uuid.uuid4()),
            },
        )

        if not response.is_success:
            try:
                error_data = response.json()
                error_msg = (
                    error_data.get("error_description")
                    or error_data.get("error")
                    or response.text
                )
            except Exception:
                error_msg = response.text
            raise DeviceAuthorizationError(f"Device authorization failed: {error_msg}")

        result = response.json()

    return DeviceCodeResponse(
        device_code=result["device_code"],
        user_code=result["user_code"],
        verification_uri=result["verification_uri"],
        verification_uri_complete=result["verification_uri_complete"],
        expires_in=result["expires_in"],
    )


async def _poll_once(device_code: str, verifier: str) -> QwenOAuthToken | None:
    """Poll the token endpoint once.

    Args:
        device_code: The device code from device authorization.
        verifier: The PKCE verifier.

    Returns:
        QwenOAuthToken if authorization is complete, None if still pending.

    Raises:
        SlowDown: If server requests slower polling.
        TokenPollingError: If polling fails with a non-recoverable error.
    """
    data = {
        "grant_type": GRANT_TYPE,
        "client_id": CLIENT_ID,
        "device_code": device_code,
        "code_verifier": verifier,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            content=urlencode(data),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "x-request-id": str(uuid.uuid4()),
            },
        )

        if response.status_code == HTTP_BAD_REQUEST:
            try:
                error_data = response.json()
                error = error_data.get("error", "")
                if error == "authorization_pending":
                    return None
            except Exception:
                pass
            raise TokenPollingError(f"Token poll failed: {response.text}")

        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            raise SlowDown("Server requested slower polling")

        if not response.is_success:
            try:
                error_data = response.json()
                error_msg = (
                    error_data.get("error_description")
                    or error_data.get("error")
                    or response.text
                )
            except Exception:
                error_msg = response.text
            raise TokenPollingError(f"Token poll failed: {error_msg}")

        token_data = response.json()

    access_token = token_data.get("access_token")
    if not access_token:
        return None

    return QwenOAuthToken.from_token_response(
        access_token=access_token,
        refresh_token=token_data.get("refresh_token", ""),
        expires_in=token_data.get("expires_in", 3600),
        resource_url=token_data.get("resource_url"),
    )


async def poll_for_token(
    device_code: str,
    verifier: str,
    expires_in: int = 900,
    initial_interval: float = 2.0,
    status_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> QwenOAuthToken:
    """Poll the token endpoint until authorization is complete.

    Args:
        device_code: The device code from device authorization.
        verifier: The PKCE verifier.
        expires_in: Timeout in seconds (default 900 = 15 minutes).
        initial_interval: Initial polling interval in seconds.
        status_callback: Optional callback to report status updates.
        cancel_check: Optional callback that returns True if polling should be cancelled.

    Returns:
        QwenOAuthToken when authorization is complete.

    Raises:
        AuthorizationTimeout: If authorization times out.
        AuthorizationCancelled: If authorization is cancelled.
        TokenPollingError: If polling fails with a non-recoverable error.
    """
    interval = initial_interval
    max_interval = 10.0
    elapsed = 0.0
    attempt = 0

    while elapsed < expires_in:
        if cancel_check and cancel_check():
            raise AuthorizationCancelled("Authorization cancelled by user")

        attempt += 1
        if status_callback:
            status_callback(f"Checking for authorization... (attempt {attempt})")

        try:
            token = await _poll_once(device_code, verifier)
            if token:
                return token
        except SlowDown:
            interval = min(interval * 1.5, max_interval)
        except TokenPollingError:
            raise

        await asyncio.sleep(interval)
        elapsed += interval

    raise AuthorizationTimeout("Authorization timed out")


async def refresh_token(refresh_token_str: str) -> QwenOAuthToken:
    """Refresh an expired access token.

    Args:
        refresh_token_str: The refresh token to use.

    Returns:
        New QwenOAuthToken with fresh access token.

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
            content=urlencode(data),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "x-request-id": str(uuid.uuid4()),
            },
        )

        if not response.is_success:
            try:
                error_data = response.json()
                error_msg = (
                    error_data.get("error_description")
                    or error_data.get("error")
                    or response.text
                )
            except Exception:
                error_msg = response.text
            raise ValueError(f"Token refresh failed: {error_msg}")

        token_data = response.json()

    return QwenOAuthToken.from_token_response(
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", refresh_token_str),
        expires_in=token_data.get("expires_in", 3600),
        resource_url=token_data.get("resource_url"),
    )
