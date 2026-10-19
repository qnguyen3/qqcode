from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from vibe.core.oauth.base import AuthInteraction
from vibe.core.oauth.callback_server import (
    OAuthCallbackServer,
    start_oauth_callback_server,
    wait_for_callback_or_manual_input,
)
from vibe.core.oauth.device_code import DeviceFlowError
from vibe.core.oauth.pkce import generate_pkce, random_base64url
from vibe.core.oauth.registry import register_flow
from vibe.core.oauth.token import OAuthToken

DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME_HINT = "QQcode"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
DIRECT_TOKEN_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {DIRECT_TOKEN_SCOPE}"


@dataclass(frozen=True)
class _Authorization:
    code: str
    client_id: str


def _result_from_params(
    params: Mapping[str, str], expected_state: str
) -> _Authorization:
    code = params.get("code")
    state = params.get("state")
    client_id = (params.get("client_id") or "").strip()
    if not code:
        raise DeviceFlowError("Missing authorization code")
    if not state:
        raise DeviceFlowError("Missing OAuth state")
    if state != expected_state:
        raise DeviceFlowError("OAuth state mismatch")
    if not client_id:
        raise DeviceFlowError(
            "OpenAI OAuth registration callback did not contain an issued client ID"
        )
    return _Authorization(code=code, client_id=client_id)


def _result_from_manual_input(value: str, expected_state: str) -> _Authorization:
    url = urlparse(value.strip())
    expected = urlparse(REDIRECT_URI)
    if (url.scheme, url.netloc, url.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise DeviceFlowError(f"The pasted callback URL must start with {REDIRECT_URI}")
    params = {key: values[0] for key, values in parse_qs(url.query).items()}
    if error := params.get("error"):
        raise DeviceFlowError(f"ChatGPT authorization failed: {error}")
    return _result_from_params(params, expected_state)


async def _request_token(body: Mapping[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL, data=dict(body), headers={"Accept": "application/json"}
        )
    if not response.is_success:
        raise DeviceFlowError(
            f"OpenAI OAuth token request failed ({response.status_code}): "
            f"{response.text}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise DeviceFlowError("OpenAI OAuth token response must be an object")
    return data


def _require_token_string(data: Mapping[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DeviceFlowError(f"OpenAI OAuth token response has invalid {field}")
    return value


def _credentials(data: Mapping[str, Any], client_id: str) -> OAuthToken:
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise DeviceFlowError("OpenAI OAuth token response has invalid expires_in")
    scope = _require_token_string(data, "scope")
    scopes = scope.split()
    if DIRECT_TOKEN_SCOPE not in scopes:
        raise DeviceFlowError(
            f"OpenAI OAuth grant did not include {DIRECT_TOKEN_SCOPE}"
        )
    return OAuthToken.from_token_response(
        _require_token_string(data, "access_token"),
        _require_token_string(data, "refresh_token"),
        int(expires_in),
        metadata={"client_id": client_id, "scopes": scopes},
    )


async def _exchange(code: str, verifier: str, client_id: str) -> OAuthToken:
    data = await _request_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "resource": RESOURCE,
    })
    id_token = data.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        raise DeviceFlowError("OpenAI OAuth token response did not contain an ID token")
    return _credentials(data, client_id)


async def _refresh(token: OAuthToken) -> OAuthToken:
    client_id = token.metadata.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise DeviceFlowError(
            "Stored ChatGPT OAuth token has no issued client ID; sign in again"
        )
    data = await _request_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": token.refresh_token,
        "resource": RESOURCE,
    })
    return _credentials(data, client_id)


async def _start_callback(
    state: str, cancel_event: Any
) -> OAuthCallbackServer[_Authorization] | None:
    try:
        return await start_oauth_callback_server(
            provider_name="ChatGPT",
            host=CALLBACK_HOST,
            port=CALLBACK_PORT,
            path=CALLBACK_PATH,
            state=state,
            complete=lambda code, params: _resolved(code, params, state),
            cancel_event=cancel_event,
        )
    except OSError:
        return None


async def _resolved(code: str, params: Mapping[str, str], state: str) -> _Authorization:
    return _result_from_params({**params, "code": code}, state)


class OpenAIChatGPTOAuthFlow:
    """OpenAI Responses access through the ChatGPT subscription sign-in."""

    name: ClassVar[str] = "openai"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Sign in with a ChatGPT plan"

    async def login(self, interaction: AuthInteraction) -> OAuthToken:
        verifier, challenge = generate_pkce()
        state = random_base64url()
        callback = await _start_callback(state, interaction.cancel_event)
        try:
            query = urlencode({
                "client_id": DYNAMIC_CLIENT_ID,
                "agent_name_hint": AGENT_NAME_HINT,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "resource": RESOURCE,
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": random_base64url(),
            })
            await interaction.show_auth_url(
                f"{AUTHORIZE_URL}?{query}",
                instructions=(
                    "Complete sign-in in your browser. If the callback does "
                    "not complete, paste the final redirect URL here."
                ),
            )
            outcome = await wait_for_callback_or_manual_input(
                interaction,
                callback,
                message=(
                    "Complete login in your browser, or paste the final "
                    "redirect URL here:"
                ),
                placeholder=REDIRECT_URI,
            )
            if outcome.from_callback and outcome.value is not None:
                authorization = outcome.value
            else:
                authorization = _result_from_manual_input(outcome.input or "", state)
            await interaction.progress("Exchanging authorization code for tokens...")
            return await _exchange(
                authorization.code, verifier, authorization.client_id
            )
        finally:
            if callback is not None:
                await callback.close()

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        return await _refresh(token)


register_flow(OpenAIChatGPTOAuthFlow())
