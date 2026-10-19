from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from vibe.core import ambient
from vibe.core.ambient import (
    AmbientCredential,
    ambient_setup,
    resolve_amazon_bedrock,
    resolve_ambient,
    resolve_cloudflare_ai_gateway,
    resolve_cloudflare_workers_ai,
    resolve_google_vertex,
    resolve_google_vertex_auth,
    vertex_access_token,
)
from vibe.core.auth import provider_auths
from vibe.core.config import (
    MissingAmbientCredentialsError,
    ModelConfig,
    ProviderConfig,
    VibeConfig,
    skip_api_key_check,
)

_AMBIENT_ENV_VARS = (
    "GOOGLE_CLOUD_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_SHARED_CREDENTIALS_FILE",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_GATEWAY_ID",
)


@pytest.fixture(autouse=True)
def _isolated_ambient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ambient, "VERTEX_ADC_PATH", tmp_path / "missing-adc.json")
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials")
    )


def _build(ambient_kind: str, name: str = "cloud") -> VibeConfig:
    provider = ProviderConfig(
        name=name,
        api_base="https://api.cloudflare.com/client/v4/accounts/x/ai/v1",
        api_style="openai",
        ambient=ambient_kind,
    )
    model = ModelConfig(name="m", provider=name, alias=f"{name}-model")
    return VibeConfig(
        active_model=f"{name}-model", providers=[provider], models=[model]
    )


def _config(ambient_kind: str, name: str = "cloud") -> VibeConfig:
    with skip_api_key_check():
        return _build(ambient_kind, name)


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Google Vertex
# --------------------------------------------------------------------------- #


def test_vertex_absent_returns_none() -> None:
    assert resolve_google_vertex() is None


def test_vertex_api_key_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", "gcp-key")

    credential = resolve_google_vertex()

    assert credential is not None
    assert credential.source == "GOOGLE_CLOUD_API_KEY"
    assert credential.api_key == "gcp-key"
    assert credential.api_base == "https://aiplatform.googleapis.com/v1"


def test_vertex_adc_requires_project_and_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _write(
        tmp_path / "adc.json", {"type": "service_account", "client_email": "a@b"}
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    credential = resolve_google_vertex()

    assert credential is not None
    assert credential.source == "GOOGLE_APPLICATION_CREDENTIALS (adc.json)"
    assert credential.api_key is None
    assert credential.api_base == (
        "https://us-central1-aiplatform.googleapis.com/v1"
        "/projects/demo/locations/us-central1"
    )


def test_vertex_adc_absent_project_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _write(tmp_path / "adc.json", {"type": "service_account"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    assert resolve_google_vertex() is None


@pytest.mark.asyncio
async def test_vertex_access_token_refreshes_authorized_user(tmp_path: Path) -> None:
    credentials = _write(
        tmp_path / "adc.json",
        {
            "type": "authorized_user",
            "client_id": "cid",
            "client_secret": "secret",
            "refresh_token": "refresh",
        },
    )

    with respx.mock() as mock_api:
        route = mock_api.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "adc-token"})
        )

        token = await vertex_access_token(credentials)

    assert token == "adc-token"
    assert route.called
    body = route.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=refresh" in body


@pytest.mark.asyncio
async def test_vertex_access_token_falls_back_to_gcloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _write(tmp_path / "adc.json", {"type": "external_account"})
    monkeypatch.setattr(
        ambient.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="gcloud-token\n"),
    )

    token = await vertex_access_token(credentials)

    assert token == "gcloud-token"


@pytest.mark.asyncio
async def test_vertex_async_auth_adds_bearer_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _write(tmp_path / "adc.json", {"type": "authorized_user"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    async def fake_token(path: Path | None = None) -> str:
        return "fake-adc-token"

    monkeypatch.setattr(ambient, "vertex_access_token", fake_token)

    credential = await resolve_google_vertex_auth()

    assert credential is not None
    assert credential.headers == {"Authorization": "Bearer fake-adc-token"}
    assert credential.api_base is not None


# --------------------------------------------------------------------------- #
# Amazon Bedrock
# --------------------------------------------------------------------------- #


def test_bedrock_absent_returns_none() -> None:
    assert resolve_amazon_bedrock() is None


def test_bedrock_bearer_token_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer")

    credential = resolve_amazon_bedrock()

    assert credential is not None
    assert credential.source == "AWS_BEARER_TOKEN_BEDROCK"
    assert credential.api_key == "bearer"


def test_bedrock_env_keys_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    credential = resolve_amazon_bedrock()

    assert credential is not None
    assert credential.source == "AWS access keys"
    assert credential.api_base == "https://bedrock-runtime.eu-west-1.amazonaws.com"


def test_bedrock_profile_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[work]\naws_access_key_id = WORKKEY\naws_secret_access_key = worksecret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setenv("AWS_PROFILE", "work")

    credential = resolve_amazon_bedrock()

    assert credential is not None
    assert credential.source == "AWS profile 'work'"


# --------------------------------------------------------------------------- #
# Cloudflare
# --------------------------------------------------------------------------- #


def test_cloudflare_workers_ai_absent_returns_none() -> None:
    assert resolve_cloudflare_workers_ai() is None


def test_cloudflare_workers_ai_resolves_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")

    credential = resolve_cloudflare_workers_ai()

    assert credential is not None
    assert credential.api_key == "cf-key"
    assert credential.api_base == (
        "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1"
    )


def test_cloudflare_ai_gateway_sets_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", "gw")

    credential = resolve_cloudflare_ai_gateway()

    assert credential is not None
    assert credential.api_key is None
    assert credential.headers == {"cf-aig-authorization": "Bearer cf-key"}
    assert credential.api_base == (
        "https://gateway.ai.cloudflare.com/v1/acct/gw/compat"
    )


def test_resolve_ambient_unknown_kind_returns_none() -> None:
    assert resolve_ambient("nope") is None
    assert resolve_ambient("") is None


def test_ambient_setup_has_instructions_for_every_kind() -> None:
    for kind in ambient.AMBIENT_RESOLVERS:
        assert ambient_setup(kind)
    assert ambient_setup("unknown")


# --------------------------------------------------------------------------- #
# provider_auths / _check_api_key integration
# --------------------------------------------------------------------------- #


def test_provider_auths_reports_unconfigured_ambient() -> None:
    config = _config("cloudflare-workers-ai")

    auth = next(a for a in provider_auths(config, "cloud") if a.kind == "ambient")

    assert not auth.configured
    assert auth.source is None
    assert auth.setup is not None


def test_provider_auths_reports_configured_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    config = _config("cloudflare-workers-ai")

    auth = next(a for a in provider_auths(config, "cloud") if a.kind == "ambient")

    assert auth.configured
    assert auth.source == "CLOUDFLARE_API_KEY"


def test_missing_ambient_credentials_raise_clear_error() -> None:
    with pytest.raises(MissingAmbientCredentialsError) as excinfo:
        _build("amazon-bedrock")

    assert excinfo.value.ambient == "amazon-bedrock"
    assert "AWS_ACCESS_KEY_ID" in str(excinfo.value)


def test_ambient_credentials_satisfy_the_api_key_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    config = _build("amazon-bedrock")

    assert config.active_model == "cloud-model"


def test_ambient_credential_defaults() -> None:
    credential = AmbientCredential(source="env")

    assert credential.api_key is None
    assert credential.headers == {}
    assert credential.api_base is None
