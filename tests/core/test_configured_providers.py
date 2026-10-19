from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.auth import configured_provider_names, provider_is_configured
from vibe.core.config import ProviderConfig, VibeConfig, skip_api_key_check
from vibe.core.oauth import OAuthToken

API_KEY_ENV = "TEST_QQCODE_API_KEY"


def _env_file_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("vibe.core.auth.GLOBAL_ENV_FILE", tmp_path / "missing.env")


def _api_key_provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name, api_base=f"https://{name}.test/v1", api_key_env_var=API_KEY_ENV
    )


def _config(*providers: ProviderConfig) -> VibeConfig:
    with skip_api_key_check():
        return VibeConfig(providers=list(providers), models=[])


def test_provider_without_auth_is_always_configured() -> None:
    config = _config(ProviderConfig(name="test-local", api_base="http://127.0.0.1/v1"))

    assert provider_is_configured(config, "test-local")
    assert "test-local" in configured_provider_names(config)


def test_unknown_provider_is_never_configured() -> None:
    config = _config(_api_key_provider("test-apikey"))

    assert not provider_is_configured(config, "nope")
    assert "nope" not in configured_provider_names(config)


def test_api_key_provider_follows_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_file_isolation(monkeypatch, tmp_path)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    config = _config(_api_key_provider("test-apikey"))

    assert not provider_is_configured(config, "test-apikey")
    assert "test-apikey" not in configured_provider_names(config)

    monkeypatch.setenv(API_KEY_ENV, "sk-test")

    assert provider_is_configured(config, "test-apikey")
    assert "test-apikey" in configured_provider_names(config)


def test_oauth_token_makes_a_provider_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_file_isolation(monkeypatch, tmp_path)
    config = _config(
        ProviderConfig(
            name="anthropic",
            api_base="https://api.anthropic.com",
            api_key_env_var="TEST_QQCODE_ANTHROPIC_KEY",
            oauth_flow="anthropic",
            oauth_token=OAuthToken(
                access_token="access",
                refresh_token="refresh",
                expires_in=3600,
                expires_at=9_999_999_999,
            ),
        )
    )

    assert provider_is_configured(config, "anthropic")
    assert "anthropic" in configured_provider_names(config)
