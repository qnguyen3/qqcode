"""Ambient (non-interactive) credential resolution for cloud providers.

These providers authenticate from credentials that already exist on the
machine (environment variables, cloud config files, CLI logins). Nothing is
prompted for or stored by QQcode; the resolvers only report what they find and
expose the material the request path needs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import subprocess
import time

import httpx

from vibe.core import aws

logger = logging.getLogger(__name__)

VERTEX_ADC_PATH = (
    Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
)
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(frozen=True)
class AmbientCredential:
    """A resolved ambient credential for one provider."""

    source: str
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    api_base: str | None = None


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


# --------------------------------------------------------------------------- #
# Google Vertex AI
# --------------------------------------------------------------------------- #


def vertex_credentials_path() -> Path | None:
    """Explicit ``GOOGLE_APPLICATION_CREDENTIALS`` or the default ADC file."""
    if explicit := _env("GOOGLE_APPLICATION_CREDENTIALS"):
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    return VERTEX_ADC_PATH if VERTEX_ADC_PATH.is_file() else None


def _vertex_base_url(project: str | None, location: str | None) -> str:
    if project and location:
        return (
            f"https://{location}-aiplatform.googleapis.com/v1"
            f"/projects/{project}/locations/{location}"
        )
    return "https://aiplatform.googleapis.com/v1"


def _vertex_source(path: Path) -> str:
    if path == VERTEX_ADC_PATH:
        return "gcloud application default credentials"
    return f"GOOGLE_APPLICATION_CREDENTIALS ({path.name})"


def resolve_google_vertex() -> AmbientCredential | None:
    """Detect a Vertex API key or Application Default Credentials (no network)."""
    if api_key := _env("GOOGLE_CLOUD_API_KEY"):
        return AmbientCredential(
            source="GOOGLE_CLOUD_API_KEY",
            api_key=api_key,
            api_base=_vertex_base_url(
                _env("GOOGLE_CLOUD_PROJECT") or _env("GCLOUD_PROJECT"),
                _env("GOOGLE_CLOUD_LOCATION"),
            ),
        )

    path = vertex_credentials_path()
    project = _env("GOOGLE_CLOUD_PROJECT") or _env("GCLOUD_PROJECT")
    location = _env("GOOGLE_CLOUD_LOCATION")
    if not (path and project and location):
        return None
    return AmbientCredential(
        source=_vertex_source(path), api_base=_vertex_base_url(project, location)
    )


def _load_credentials(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _access_token(payload: object) -> str | None:
    if isinstance(payload, dict):
        token = payload.get("access_token")
        if isinstance(token, str):
            return token
    return None


async def _service_account_token(
    client: httpx.AsyncClient, data: dict[str, object]
) -> str | None:
    try:
        import jwt
    except ImportError:
        return None

    email = data.get("client_email")
    private_key = data.get("private_key")
    if not (isinstance(email, str) and isinstance(private_key, str)):
        return None
    token_uri = data.get("token_uri")
    uri = token_uri if isinstance(token_uri, str) else _GOOGLE_TOKEN_URI

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": email,
            "scope": _GOOGLE_SCOPE,
            "aud": uri,
            "iat": now,
            "exp": now + 3600,
        },
        private_key,
        algorithm="RS256",
    )
    response = await client.post(
        uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    response.raise_for_status()
    return _access_token(response.json())


async def _authorized_user_token(
    client: httpx.AsyncClient, data: dict[str, object]
) -> str | None:
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    refresh_token = data.get("refresh_token")
    if not (
        isinstance(client_id, str)
        and isinstance(client_secret, str)
        and isinstance(refresh_token, str)
    ):
        return None
    response = await client.post(
        _GOOGLE_TOKEN_URI,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    response.raise_for_status()
    return _access_token(response.json())


def _gcloud_access_token() -> str | None:
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token or None


async def vertex_access_token(path: Path | None = None) -> str | None:
    """Fetch an OAuth access token for ADC, or ``None`` when unavailable.

    Tries the credentials JSON first (service account or authorized user) and
    falls back to ``gcloud auth application-default print-access-token``.
    """
    credentials_path = path or vertex_credentials_path()
    if credentials_path is None:
        return None

    data = _load_credentials(credentials_path)
    async with httpx.AsyncClient(timeout=15.0) as client:
        match data:
            case {"type": "service_account"}:
                token = await _service_account_token(client, data)
            case {"type": "authorized_user"}:
                token = await _authorized_user_token(client, data)
            case _:
                token = None
        if token:
            return token

    return _gcloud_access_token()


async def resolve_google_vertex_auth() -> AmbientCredential | None:
    """Vertex credential including a freshly minted bearer token for ADC."""
    credential = resolve_google_vertex()
    if credential is None or credential.api_key:
        return credential

    token = await vertex_access_token()
    if not token:
        return None
    return AmbientCredential(
        source=credential.source,
        headers={"Authorization": f"Bearer {token}"},
        api_base=credential.api_base,
    )


# --------------------------------------------------------------------------- #
# Amazon Bedrock
# --------------------------------------------------------------------------- #


def _bedrock_base_url() -> str:
    return f"https://bedrock-runtime.{aws.resolve_region()}.amazonaws.com"


def resolve_amazon_bedrock() -> AmbientCredential | None:
    """Detect AWS credentials from the environment or a named profile."""
    if token := _env("AWS_BEARER_TOKEN_BEDROCK"):
        return AmbientCredential(
            source="AWS_BEARER_TOKEN_BEDROCK",
            api_key=token,
            api_base=_bedrock_base_url(),
        )
    if credentials := aws.load_aws_credentials():
        _, source = credentials
        return AmbientCredential(source=source, api_base=_bedrock_base_url())
    return None


# --------------------------------------------------------------------------- #
# Cloudflare
# --------------------------------------------------------------------------- #


def resolve_cloudflare_workers_ai() -> AmbientCredential | None:
    """Detect a Cloudflare Workers AI key and account id."""
    api_key = _env("CLOUDFLARE_API_KEY")
    account_id = _env("CLOUDFLARE_ACCOUNT_ID")
    if not (api_key and account_id):
        return None
    return AmbientCredential(
        source="CLOUDFLARE_API_KEY",
        api_key=api_key,
        api_base=f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
    )


def resolve_cloudflare_ai_gateway() -> AmbientCredential | None:
    """Detect a Cloudflare AI Gateway key, account id and gateway id."""
    api_key = _env("CLOUDFLARE_API_KEY")
    account_id = _env("CLOUDFLARE_ACCOUNT_ID")
    gateway_id = _env("CLOUDFLARE_GATEWAY_ID")
    if not (api_key and account_id and gateway_id):
        return None
    return AmbientCredential(
        source="CLOUDFLARE_API_KEY",
        headers={"cf-aig-authorization": f"Bearer {api_key}"},
        api_base=(
            f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}/compat"
        ),
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

AMBIENT_RESOLVERS: dict[str, Callable[[], AmbientCredential | None]] = {
    "google-vertex": resolve_google_vertex,
    "amazon-bedrock": resolve_amazon_bedrock,
    "cloudflare-workers-ai": resolve_cloudflare_workers_ai,
    "cloudflare-ai-gateway": resolve_cloudflare_ai_gateway,
}

_ASYNC_RESOLVERS: dict[str, Callable[[], Awaitable[AmbientCredential | None]]] = {
    "google-vertex": resolve_google_vertex_auth
}

AMBIENT_SETUP: dict[str, str] = {
    "google-vertex": (
        "Set GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION, then run "
        "`gcloud auth application-default login` (or point "
        "GOOGLE_APPLICATION_CREDENTIALS at a service-account JSON)."
    ),
    "amazon-bedrock": (
        "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, configure a profile "
        "in ~/.aws/credentials (with AWS_PROFILE), or set "
        "AWS_BEARER_TOKEN_BEDROCK. AWS_REGION selects the endpoint."
    ),
    "cloudflare-workers-ai": "Set CLOUDFLARE_API_KEY and CLOUDFLARE_ACCOUNT_ID.",
    "cloudflare-ai-gateway": (
        "Set CLOUDFLARE_API_KEY, CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_GATEWAY_ID."
    ),
}


def resolve_ambient(ambient: str) -> AmbientCredential | None:
    """Resolve an ambient credential from local configuration, if present."""
    resolver = AMBIENT_RESOLVERS.get(ambient)
    return resolver() if resolver else None


async def resolve_ambient_auth(ambient: str) -> AmbientCredential | None:
    """Resolve an ambient credential, minting short-lived tokens when needed."""
    if resolver := _ASYNC_RESOLVERS.get(ambient):
        return await resolver()
    return resolve_ambient(ambient)


def ambient_setup(ambient: str) -> str:
    """Human-readable setup instructions for an ambient provider."""
    return AMBIENT_SETUP.get(
        ambient, "Configure the provider's ambient credentials on this machine."
    )
