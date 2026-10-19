from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import ClassVar

from dotenv import dotenv_values
import pytest

from vibe.cli import login as login_cli
from vibe.core.auth import (
    all_provider_auths,
    logout,
    provider_auths,
    remove_api_key,
    save_api_key,
)
from vibe.core.config import (
    Backend,
    MissingAPIKeyError,
    ModelConfig,
    ProviderConfig,
    TomlFileSettingsSource,
    VibeConfig,
    clear_oauth_token,
    config_file_override,
    skip_api_key_check,
)
from vibe.core.oauth import AnthropicOAuthToken, OAuthToken, register_flow


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    monkeypatch.setattr("vibe.core.auth.GLOBAL_ENV_FILE", path)
    return path


@pytest.fixture
def config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        workdir=tmp_path,
        providers=[
            ProviderConfig(
                name="mistral",
                api_base="https://api.mistral.ai/v1",
                api_key_env_var="MISTRAL_API_KEY",
                backend=Backend.MISTRAL,
            ),
            ProviderConfig(
                name="anthropic",
                api_base="https://api.anthropic.com",
                api_key_env_var="ANTHROPIC_API_KEY",
                api_style="anthropic",
            ),
        ],
    )


def test_provider_auths_lists_oauth_and_api_key(config: VibeConfig) -> None:
    kinds = {auth.kind for auth in provider_auths(config, "anthropic")}
    assert kinds == {"oauth", "api_key"}

    mistral = provider_auths(config, "mistral")
    assert [auth.kind for auth in mistral] == ["api_key"]
    assert mistral[0].env_var == "MISTRAL_API_KEY"


def test_unknown_provider_has_no_auths(config: VibeConfig) -> None:
    assert provider_auths(config, "nope") == []


def test_api_key_source_reports_env_and_dotenv(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert provider_auths(config, "mistral")[0].source is None

    env_file.write_text("MISTRAL_API_KEY=from-file\n", encoding="utf-8")
    assert provider_auths(config, "mistral")[0].source == ".env: MISTRAL_API_KEY"

    monkeypatch.setenv("MISTRAL_API_KEY", "from-env")
    assert provider_auths(config, "mistral")[0].source == "env: MISTRAL_API_KEY"


def test_oauth_token_reports_configured(config: VibeConfig) -> None:
    provider = config.providers[1]
    provider.oauth_token = AnthropicOAuthToken.from_token_response(
        "access", "refresh", 3600
    )

    oauth = next(
        auth for auth in provider_auths(config, "anthropic") if auth.kind == "oauth"
    )

    assert oauth.configured
    assert oauth.source == "OAuth token"
    assert oauth.subscription


def test_save_api_key_writes_dotenv_and_environment(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    provider = config.providers[0]

    path = save_api_key(provider, "secret-key")

    assert path == env_file
    assert dotenv_values(env_file)["MISTRAL_API_KEY"] == "secret-key"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert os.environ["MISTRAL_API_KEY"] == "secret-key"


def test_remove_api_key_clears_dotenv_and_environment(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    provider = config.providers[0]
    save_api_key(provider, "secret-key")

    assert remove_api_key(provider) is True

    assert "MISTRAL_API_KEY" not in dotenv_values(env_file)
    assert "MISTRAL_API_KEY" not in os.environ
    assert remove_api_key(provider) is False


def test_clear_oauth_token_removes_it_from_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[[providers]]\n"
        'name = "anthropic"\n'
        'api_base = "https://api.anthropic.com"\n'
        'api_key_env_var = "ANTHROPIC_API_KEY"\n'
        "[providers.oauth_token]\n"
        'access_token = "access"\n'
        'refresh_token = "refresh"\n'
        "expires_in = 3600\n"
        "expires_at = 9999999999\n",
        encoding="utf-8",
    )

    with config_file_override(config_file):
        assert clear_oauth_token("anthropic") is True
        assert clear_oauth_token("anthropic") is False

    assert "oauth_token" not in config_file.read_text(encoding="utf-8")


def test_logout_removes_every_credential(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    provider = config.providers[0]
    save_api_key(provider, "secret-key")

    notes = logout(config, "mistral")

    assert len(notes) == 1
    assert "MISTRAL_API_KEY" in notes[0]
    assert logout(config, "mistral") == []
    assert all_provider_auths(config, "api_key")[0].source is None


def test_login_command_saves_key_for_named_provider(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(VibeConfig, "load", classmethod(lambda cls, **_: config))
    monkeypatch.setattr(login_cli.Prompt, "ask", lambda *args, **kwargs: "typed-key")

    exit_code = login_cli.run_login_command(["mistral"])

    assert exit_code == 0
    assert dotenv_values(env_file)["MISTRAL_API_KEY"] == "typed-key"


def test_login_command_prints_ambient_setup(
    config: VibeConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(VibeConfig, "load", classmethod(lambda cls, **_: config))

    assert login_cli.run_login_command(["cloudflare-workers-ai"]) == 0

    output = capsys.readouterr().out
    assert "ambient credentials" in output
    assert "CLOUDFLARE_API_KEY" in output


def test_login_command_dispatches_oauth_flows(
    config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VibeConfig, "load", classmethod(lambda cls, **_: config))
    called: list[str] = []
    monkeypatch.setattr(
        login_cli, "login_anthropic", lambda: called.append("anthropic")
    )
    monkeypatch.setattr(login_cli.Prompt, "ask", lambda *args, **kwargs: "1")

    assert login_cli.run_login_command(["anthropic"]) == 0

    assert called == ["anthropic"]


def test_login_command_interactive_flow(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(VibeConfig, "load", classmethod(lambda cls, **_: config))
    answers = iter(["2", "1", "interactive-key"])
    monkeypatch.setattr(login_cli.Prompt, "ask", lambda *args, **kwargs: next(answers))

    assert login_cli.run_login_command([]) == 0

    assert dotenv_values(env_file)["MISTRAL_API_KEY"] == "interactive-key"


def test_logout_command_removes_named_credential(
    config: VibeConfig, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    save_api_key(config.providers[0], "secret-key")
    monkeypatch.setattr(VibeConfig, "load", classmethod(lambda cls, **_: config))

    assert login_cli.run_logout_command(["mistral"]) == 0

    assert "MISTRAL_API_KEY" not in dotenv_values(env_file)


def test_skip_api_key_check_allows_config_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    providers = [
        ProviderConfig(
            name="acme", api_base="https://acme.test/v1", api_key_env_var="ACME_API_KEY"
        )
    ]
    models = [ModelConfig(name="acme-1", provider="acme", alias="acme-1")]

    with pytest.raises(MissingAPIKeyError):
        VibeConfig(active_model="acme-1", providers=providers, models=models)

    with skip_api_key_check():
        config = VibeConfig(active_model="acme-1", providers=providers, models=models)

    assert config.active_model == "acme-1"


def test_login_loads_config_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    providers = [
        ProviderConfig(
            name="acme", api_base="https://acme.test/v1", api_key_env_var="ACME_API_KEY"
        )
    ]
    models = [ModelConfig(name="acme-1", provider="acme", alias="acme-1")]

    def fake_load(cls: type[VibeConfig], **_: object) -> VibeConfig:
        return VibeConfig(active_model="acme-1", providers=providers, models=models)

    monkeypatch.setattr(VibeConfig, "load", classmethod(fake_load))

    config = login_cli._load_login_config()

    assert config.active_model == "acme-1"


class _RegistryRefreshFlow:
    name: ClassVar[str] = "test-registry-flow"
    is_subscription: ClassVar[bool] = True
    login_label: ClassVar[str] = "Test"

    def __init__(self) -> None:
        self.calls = 0

    async def login(self, interaction: object) -> OAuthToken:
        raise NotImplementedError

    async def refresh(self, token: OAuthToken) -> AnthropicOAuthToken:
        self.calls += 1
        return AnthropicOAuthToken.from_token_response(
            "fresh", token.refresh_token, 3600
        )


def test_check_api_key_refreshes_via_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _RegistryRefreshFlow()
    register_flow(flow)
    saved: list[tuple[str, OAuthToken]] = []
    monkeypatch.setattr(
        "vibe.core.config.save_oauth_token",
        lambda name, token: saved.append((name, token)),
    )
    provider = ProviderConfig(
        name="acme-oauth",
        api_base="https://acme.test/v1",
        oauth_flow="test-registry-flow",
        oauth_token=OAuthToken(
            access_token="old",
            refresh_token="refresh-me",
            expires_in=3600,
            expires_at=int(time.time()) - 1,
        ),
    )
    model = ModelConfig(name="m", provider="acme-oauth", alias="acme-oauth-1")

    config = VibeConfig(
        active_model="acme-oauth-1", providers=[provider], models=[model]
    )

    token = next(p for p in config.providers if p.name == "acme-oauth").oauth_token
    assert token is not None
    assert token.access_token == "fresh"
    assert flow.calls == 1
    assert saved == [("acme-oauth", token)]


def test_legacy_anthropic_token_config_loads(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'active_model = "claude-x"\n'
        "[[providers]]\n"
        'name = "anthropic"\n'
        'api_base = "https://api.anthropic.com"\n'
        'api_key_env_var = "ANTHROPIC_API_KEY"\n'
        'api_style = "anthropic"\n'
        'backend = "anthropic"\n'
        "[providers.oauth_token]\n"
        'access_token = "legacy-access"\n'
        'refresh_token = "legacy-refresh"\n'
        "expires_in = 3600\n"
        "expires_at = 9999999999\n"
        "[[models]]\n"
        'name = "claude-x"\n'
        'provider = "anthropic"\n'
        'alias = "claude-x"\n',
        encoding="utf-8",
    )

    with config_file_override(config_file):
        file_data = TomlFileSettingsSource(VibeConfig).toml_data
    config = VibeConfig(**file_data)

    provider = next(p for p in config.providers if p.name == "anthropic")
    assert provider.oauth_token is not None
    assert provider.oauth_token.access_token == "legacy-access"
    assert provider.oauth_token.refresh_token == "legacy-refresh"


def test_legacy_qwen_resource_url_config_loads(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'active_model = "qwen-x"\n'
        "[[providers]]\n"
        'name = "qwen"\n'
        'api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"\n'
        'api_key_env_var = "DASHSCOPE_API_KEY"\n'
        "[providers.oauth_token]\n"
        'access_token = "qwen-access"\n'
        'refresh_token = "qwen-refresh"\n'
        "expires_in = 3600\n"
        "expires_at = 9999999999\n"
        'resource_url = "portal.qwen.ai"\n'
        "[[models]]\n"
        'name = "qwen3-coder-plus"\n'
        'provider = "qwen"\n'
        'alias = "qwen-x"\n',
        encoding="utf-8",
    )

    with config_file_override(config_file):
        file_data = TomlFileSettingsSource(VibeConfig).toml_data
    config = VibeConfig(**file_data)

    provider = next(p for p in config.providers if p.name == "qwen")
    assert provider.oauth_token is not None
    assert provider.oauth_token.resource_url == "portal.qwen.ai"


def test_unregistered_oauth_provider_does_not_crash() -> None:
    provider = ProviderConfig(
        name="mystery",
        api_base="https://mystery.test/v1",
        oauth_token=OAuthToken(
            access_token="a",
            refresh_token="r",
            expires_in=3600,
            expires_at=int(time.time()) - 10,
        ),
    )
    model = ModelConfig(name="m", provider="mystery", alias="mystery-1")

    config = VibeConfig(active_model="mystery-1", providers=[provider], models=[model])

    token = next(p for p in config.providers if p.name == "mystery").oauth_token
    assert token is not None
    assert token.access_token == "a"


def test_entrypoint_routes_login_and_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibe.cli import entrypoint

    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        entrypoint, "run_login_command", lambda argv: calls.append(("login", argv)) or 0
    )
    monkeypatch.setattr(
        entrypoint,
        "run_logout_command",
        lambda argv: calls.append(("logout", argv)) or 0,
    )

    monkeypatch.setattr(sys, "argv", ["qqcode", "login", "mistral"])
    with pytest.raises(SystemExit):
        entrypoint._handle_subcommands()

    monkeypatch.setattr(sys, "argv", ["qqcode", "logout"])
    with pytest.raises(SystemExit):
        entrypoint._handle_subcommands()

    assert calls == [("login", ["mistral"]), ("logout", [])]
