from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tomllib

import pytest
import tomli_w

from vibe.core import config
from vibe.core.config import (
    CompactionConfig,
    HooksConfig,
    MCPStdio,
    MCPStreamableHttp,
    ModelCapabilities,
    ModelConfig,
    ModelPricing,
    VibeConfig,
)


def _restore_dump_config(config_file: Path):
    original_dump_config = VibeConfig.dump_config

    def real_dump_config(cls, config_dict: dict) -> None:
        try:
            with config_file.open("wb") as f:
                tomli_w.dump(config_dict, f)
        except OSError:
            config_file.write_text(
                "\n".join(
                    f"{k} = {v!r}" for k, v in config_dict.items() if v is not None
                ),
                encoding="utf-8",
            )

    VibeConfig.dump_config = classmethod(real_dump_config)  # type: ignore[assignment]
    return original_dump_config


@contextmanager
def _migrate_config_file(tmp_path: Path, content: str):
    config_file = tmp_path / "config.toml"
    config_file.write_text(content, encoding="utf-8")

    original_config_file = config.CONFIG_FILE
    original_dump_config = _restore_dump_config(config_file)

    try:
        config.CONFIG_FILE = config_file
        VibeConfig._migrate()
        yield config_file
    finally:
        config.CONFIG_FILE = original_config_file
        VibeConfig.dump_config = original_dump_config


def _load_migrated_config(config_file: Path) -> dict:
    with config_file.open("rb") as f:
        return tomllib.load(f)


def test_default_compaction_config() -> None:
    config = VibeConfig()

    assert config.compaction.enabled is True
    assert config.compaction.reserve_tokens == 16_384
    assert config.compaction.keep_recent_tokens == 20_000
    assert config.compaction.threshold_ratio == pytest.approx(0.90)
    assert config.compaction.prompt == "summary"
    assert config.context_warnings is True


def test_old_config_file_without_compaction_still_loads(tmp_path: Path) -> None:
    content = 'active_model = "devstral-2"\nauto_compact_threshold = 100000\n'

    with _migrate_config_file(tmp_path, content) as config_file:
        data = _load_migrated_config(config_file)
        assert data["auto_compact_threshold"] == 200_000
        assert "compaction" not in data
        config = VibeConfig(**data)

    assert config.compaction == CompactionConfig()
    assert config.context_warnings is True


def test_migrate_keeps_existing_compaction_section(tmp_path: Path) -> None:
    content = "auto_compact_threshold = 200000\n\n[compaction]\nenabled = false\n"

    with _migrate_config_file(tmp_path, content) as config_file:
        data = _load_migrated_config(config_file)

    assert data["compaction"]["enabled"] is False


def test_compaction_section_is_parsed() -> None:
    config = VibeConfig(
        compaction=CompactionConfig(enabled=False, keep_recent_tokens=5)
    )

    assert config.compaction.enabled is False
    assert config.compaction.keep_recent_tokens == 5
    assert config.compaction.reserve_tokens == 16_384


def test_get_compaction_threshold_reserves_tokens() -> None:
    model = ModelConfig(
        name="test-model",
        provider="mistral",
        alias="test-model",
        context_window=100_000,
    )
    config = VibeConfig(active_model="test-model", models=[model])

    assert (
        config.get_compaction_threshold() == 100_000 - config.compaction.reserve_tokens
    )


def test_get_compaction_threshold_uses_max_output_tokens_when_larger() -> None:
    model = ModelConfig(
        name="test-model",
        provider="mistral",
        alias="test-model",
        context_window=100_000,
        max_output_tokens=32_000,
    )
    config = VibeConfig(active_model="test-model", models=[model])

    assert config.get_compaction_threshold() == 100_000 - 32_000


def test_legacy_context_limit_maps_to_context_window() -> None:
    model = ModelConfig.model_validate({
        "name": "test-model",
        "provider": "mistral",
        "alias": "test-model",
        "context_limit": 100_000,
    })

    assert model.context_window == 100_000
    assert model.context_limit == 100_000


def test_legacy_prices_map_to_pricing() -> None:
    model = ModelConfig.model_validate({
        "name": "test-model",
        "provider": "mistral",
        "alias": "test-model",
        "input_price": 0.4,
        "output_price": 2.0,
    })

    assert model.pricing.input_per_million == 0.4
    assert model.pricing.output_per_million == 2.0
    assert model.input_price == 0.4
    assert model.output_price == 2.0


def test_legacy_price_mapping_does_not_override_pricing_fields() -> None:
    model = ModelConfig.model_validate({
        "name": "test-model",
        "provider": "mistral",
        "alias": "test-model",
        "input_price": 9.0,
        "output_price": 9.0,
        "pricing": ModelPricing(input_per_million=0.4, cache_read_per_million=0.04),
    })

    assert model.pricing.input_per_million == 0.4
    assert model.pricing.output_per_million == 9.0
    assert model.pricing.cache_read_per_million == 0.04


def test_new_schema_fields_are_preserved() -> None:
    model = ModelConfig(
        name="test-model",
        provider="mistral",
        alias="test-model",
        context_window=250_000,
        max_output_tokens=64_000,
        capabilities=ModelCapabilities(
            reasoning=True, vision=True, prompt_cache="long"
        ),
        pricing=ModelPricing(
            input_per_million=3.0,
            output_per_million=15.0,
            cache_read_per_million=0.3,
            cache_write_per_million=3.75,
        ),
    )

    assert model.context_window == 250_000
    assert model.max_output_tokens == 64_000
    assert model.capabilities.prompt_cache == "long"
    assert model.capabilities.reasoning is True
    assert model.pricing.cache_write_per_million == 3.75


def test_config_with_legacy_model_dicts_still_loads() -> None:
    config = VibeConfig.model_validate({
        "active_model": "test-model",
        "models": [
            {
                "name": "test-model",
                "provider": "mistral",
                "alias": "test-model",
                "context_limit": 100_000,
                "input_price": 0.4,
                "output_price": 2.0,
            }
        ],
    })

    model = config.get_active_model()
    assert model.context_window == 100_000
    assert config.get_context_limit_for_active_model() == 100_000
    assert model.pricing.input_per_million == 0.4
    assert model.pricing.output_per_million == 2.0


def test_old_config_without_hooks_and_packages_still_loads(tmp_path: Path) -> None:
    content = 'active_model = "devstral-2"\n'

    with _migrate_config_file(tmp_path, content) as config_file:
        data = _load_migrated_config(config_file)
        loaded = VibeConfig(**data)

    assert loaded.hooks == HooksConfig()
    assert loaded.hooks.enabled is True
    assert loaded.hooks.timeout == 5.0
    assert loaded.packages == []


def test_hooks_section_is_parsed() -> None:
    loaded = VibeConfig(
        hooks=HooksConfig(enabled=False, timeout=1.5, paths=["/tmp/hooks"])
    )

    assert loaded.hooks.enabled is False
    assert loaded.hooks.timeout == 1.5
    assert loaded.hooks.paths == ["/tmp/hooks"]


def test_packages_section_is_parsed() -> None:
    loaded = VibeConfig(packages=["alpha", "beta"])

    assert loaded.packages == ["alpha", "beta"]


def test_set_packages_persists_to_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'active_model = "devstral-2"\nauto_compact_threshold = 200000\n',
        encoding="utf-8",
    )
    original_dump_config = _restore_dump_config(config_file)
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    try:
        VibeConfig.set_packages(["alpha", "beta"])

        assert _load_migrated_config(config_file)["packages"] == ["alpha", "beta"]
        assert _load_migrated_config(config_file)["active_model"] == "devstral-2"
    finally:
        VibeConfig.dump_config = original_dump_config


def test_legacy_mcp_server_config_still_loads() -> None:
    loaded = VibeConfig.model_validate({
        "mcp_servers": [
            {
                "name": "legacy",
                "transport": "stdio",
                "command": "python",
                "args": ["server.py"],
            }
        ]
    })

    server = loaded.mcp_servers[0]
    assert server.enabled is True
    assert server.timeout == 60.0
    assert server.tool_exposure == "direct"


def test_mcp_http_oauth_fields_are_parsed() -> None:
    loaded = VibeConfig.model_validate({
        "mcp_servers": [
            {
                "name": "protected",
                "transport": "streamable-http",
                "url": "https://example.invalid/mcp",
                "timeout": 5.0,
                "tool_exposure": "deferred",
                "oauth": {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "callback_port": 3334,
                    "auth_server_url": "https://auth.example.invalid",
                    "scopes": ["read", "write"],
                },
            }
        ]
    })

    server = loaded.mcp_servers[0]
    assert isinstance(server, MCPStreamableHttp)
    assert server.timeout == 5.0
    assert server.tool_exposure == "deferred"
    assert server.oauth is not None
    assert server.oauth.client_id == "cid"
    assert server.oauth.callback_port == 3334
    assert server.oauth.scopes == ["read", "write"]


def test_mcp_stdio_env_is_parsed() -> None:
    loaded = VibeConfig.model_validate({
        "mcp_servers": [
            {
                "name": "env-server",
                "transport": "stdio",
                "command": "python",
                "env": {"API_TOKEN": "abc"},
            }
        ]
    })

    server = loaded.mcp_servers[0]
    assert isinstance(server, MCPStdio)
    assert server.env == {"API_TOKEN": "abc"}
    assert server.stdio_env()["API_TOKEN"] == "abc"


def test_missing_tools_script_section_defaults_disabled() -> None:
    from vibe.core.tools.manager import ToolManager

    loaded = VibeConfig()
    assert loaded.tool_runtime.script.enabled is False
    assert loaded.tool_runtime.script.timeout == 30.0
    assert loaded.tool_runtime.script.max_calls == 20
    assert loaded.tool_runtime.script.max_output_bytes == 16384
    assert loaded.tool_runtime.script.memory_mb == 256
    assert "run_tools_script" not in ToolManager(loaded).available_tools()


def test_tools_script_section_hoists_and_registers_when_enabled() -> None:
    from vibe.core.tools.manager import ToolManager

    loaded = VibeConfig.model_validate({
        "tools": {
            "script": {
                "enabled": True,
                "timeout": 15.0,
                "max_calls": 10,
                "max_output_bytes": 4096,
                "memory_mb": 128,
            }
        }
    })
    assert loaded.tool_runtime.script.enabled is True
    assert loaded.tool_runtime.script.timeout == 15.0
    assert loaded.tool_runtime.script.max_calls == 10
    assert loaded.tool_runtime.script.max_output_bytes == 4096
    assert loaded.tool_runtime.script.memory_mb == 128
    assert "script" not in loaded.tools
    assert "run_tools_script" in ToolManager(loaded).available_tools()
