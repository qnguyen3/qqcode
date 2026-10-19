from __future__ import annotations

from pathlib import Path
import stat
import tomllib
from typing import Any

import pytest

import vibe.core.config as config_module
from vibe.core.config import save_oauth_token
from vibe.core.oauth.token import AnthropicOAuthToken


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text('active_model = "devstral-2"\n')
    monkeypatch.setattr(config_module, "CONFIG_FILE", path)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    return path


def _token() -> AnthropicOAuthToken:
    return AnthropicOAuthToken.from_token_response(
        access_token="access", refresh_token="refresh", expires_in=3600
    )


def _leftover_temp_files(config_file: Path) -> list[Path]:
    return [
        path
        for path in config_file.parent.iterdir()
        if path.name.startswith(f".{config_file.name}.")
    ]


def test_save_writes_token_atomically_with_owner_only_permissions(
    config_file: Path,
) -> None:
    save_oauth_token("anthropic", _token())

    data = tomllib.loads(config_file.read_text())
    assert data["active_model"] == "devstral-2"
    provider = next(p for p in data["providers"] if p["name"] == "anthropic")
    assert provider["oauth_token"]["access_token"] == "access"
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert _leftover_temp_files(config_file) == []


def test_existing_provider_is_updated_in_place(config_file: Path) -> None:
    config_file.write_text(
        '[[providers]]\nname = "anthropic"\napi_base = "https://api.anthropic.com"\n'
    )

    save_oauth_token("anthropic", _token())

    data = tomllib.loads(config_file.read_text())
    assert len(data["providers"]) == 1
    assert data["providers"][0]["oauth_token"]["refresh_token"] == "refresh"


def test_crash_mid_write_keeps_previous_config(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_file.read_bytes()

    def exploding_dump(data: object, file: Any) -> None:
        file.write(b"partial garbage = [")
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(config_module.tomli_w, "dump", exploding_dump)

    with pytest.raises(RuntimeError, match="disk on fire"):
        save_oauth_token("anthropic", _token())

    assert config_file.read_bytes() == original
    assert _leftover_temp_files(config_file) == []
