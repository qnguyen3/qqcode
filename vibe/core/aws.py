"""AWS credential resolution and SigV4 request signing.

Only the environment and the shared credentials file are supported; container
credentials, web identity tokens and SSO profiles are intentionally out of scope
and reported as unconfigured by :mod:`vibe.core.ambient`.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

_ALGORITHM = "AWS4-HMAC-SHA256"
DEFAULT_REGION = "us-east-1"
_MIN_HOST_PARTS = 3


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str
    session_token: str | None = None


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def credentials_file() -> Path:
    """Location of the shared credentials file (``AWS_SHARED_CREDENTIALS_FILE``)."""
    if override := _env("AWS_SHARED_CREDENTIALS_FILE"):
        return Path(override).expanduser()
    return Path.home() / ".aws" / "credentials"


def load_profile_credentials(
    profile: str, path: Path | None = None
) -> AwsCredentials | None:
    """Read one profile from the shared credentials file, if it is complete."""
    file = path or credentials_file()
    if not file.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(file)
    except (configparser.Error, OSError):
        return None
    if not parser.has_section(profile):
        return None

    access_key = parser.get(profile, "aws_access_key_id", fallback="").strip()
    secret_key = parser.get(profile, "aws_secret_access_key", fallback="").strip()
    if not (access_key and secret_key):
        return None
    token = parser.get(profile, "aws_session_token", fallback="").strip() or None
    return AwsCredentials(access_key, secret_key, token)


def load_aws_credentials() -> tuple[AwsCredentials, str] | None:
    """Resolve credentials from environment variables or a named profile."""
    access_key = _env("AWS_ACCESS_KEY_ID")
    secret_key = _env("AWS_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        return (
            AwsCredentials(access_key, secret_key, _env("AWS_SESSION_TOKEN")),
            "AWS access keys",
        )

    profile = _env("AWS_PROFILE") or "default"
    if credentials := load_profile_credentials(profile):
        return credentials, f"AWS profile '{profile}'"
    return None


def resolve_region() -> str:
    """Region from the environment, falling back to the Bedrock default."""
    return _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or DEFAULT_REGION


def region_from_endpoint(url: str) -> str | None:
    """Extract the region from a standard Bedrock runtime endpoint."""
    host = urlsplit(url).hostname or ""
    parts = host.split(".")
    if len(parts) >= _MIN_HOST_PARTS and parts[0].startswith("bedrock-runtime"):
        return parts[1]
    return None


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key for a scope."""
    key = f"AWS4{secret_key}".encode()
    return _sign(_sign(_sign(_sign(key, datestamp), region), service), "aws4_request")


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = sorted(
        (quote(key, safe="-_.~"), quote(value, safe="-_.~")) for key, value in pairs
    )
    return "&".join(f"{key}={value}" for key, value in encoded)


def _canonical_uri(path: str) -> str:
    return quote(path or "/", safe="/-_.~")


def sign_request(
    *,
    method: str,
    url: str,
    body: bytes,
    credentials: AwsCredentials,
    region: str,
    service: str = "bedrock",
    extra_headers: dict[str, str] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    """Return SigV4 headers (including ``Authorization``) for a request.

    ``host`` is signed but omitted from the result so the HTTP client sets it.
    """
    parsed = urlsplit(url)
    amz_date = (timestamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]

    headers: dict[str, str] = {
        "content-type": "application/json",
        "host": parsed.netloc,
        "x-amz-date": amz_date,
    }
    if credentials.session_token:
        headers["x-amz-security-token"] = credentials.session_token
    for name, value in (extra_headers or {}).items():
        headers[name.lower()] = value

    signed_header_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in signed_header_names
    )
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join([
        method,
        _canonical_uri(parsed.path),
        _canonical_query(parsed.query),
        canonical_headers,
        signed_headers,
        hashlib.sha256(body).hexdigest(),
    ])

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        signing_key(credentials.secret_key, datestamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    result = {name: value for name, value in headers.items() if name != "host"}
    result["Authorization"] = (
        f"{_ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result
