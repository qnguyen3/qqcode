from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest

from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.models_overlay import (
    load_model_overlay,
    merge_model_overlays,
    validate_models,
)

OVERLAY_TOML = """
[[models]]
name = "claude-sonnet-5-0"
provider = "anthropic"
alias = "claude-sonnet-5"
context_window = 250000
max_output_tokens = 64000

[models.capabilities]
reasoning = true
vision = true
prompt_cache = "long"

[models.pricing]
input_per_million = 3.0
output_per_million = 15.0
cache_read_per_million = 0.3
cache_write_per_million = 3.75
"""


def _write_overlay(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _provider(name: str = "anthropic") -> ProviderConfig:
    return ProviderConfig(name=name, api_base="https://api.anthropic.com")


class TestLoadModelOverlay:
    def test_loads_models_from_toml(self, tmp_path: Path) -> None:
        path = _write_overlay(tmp_path / "models.toml", OVERLAY_TOML)

        models = load_model_overlay([path])

        assert len(models) == 1
        model = models[0]
        assert model.alias == "claude-sonnet-5"
        assert model.context_window == 250_000
        assert model.max_output_tokens == 64_000
        assert model.capabilities.prompt_cache == "long"
        assert model.pricing.cache_write_per_million == 3.75

    def test_missing_files_are_skipped(self, tmp_path: Path) -> None:
        assert load_model_overlay([tmp_path / "missing.toml"]) == []

    def test_legacy_fields_in_overlay_are_mapped(self, tmp_path: Path) -> None:
        path = _write_overlay(
            tmp_path / "models.toml",
            """
[[models]]
name = "legacy"
provider = "anthropic"
context_limit = 12345
input_price = 1.0
output_price = 2.0
""",
        )

        model = load_model_overlay([path])[0]

        assert model.context_window == 12_345
        assert model.pricing.input_per_million == 1.0
        assert model.pricing.output_per_million == 2.0

    def test_duplicate_alias_in_file_raises(self, tmp_path: Path) -> None:
        path = _write_overlay(
            tmp_path / "models.toml",
            """
[[models]]
name = "one"
provider = "anthropic"
alias = "same"

[[models]]
name = "two"
provider = "anthropic"
alias = "same"
""",
        )

        with pytest.raises(ValueError, match="Duplicate model alias"):
            load_model_overlay([path])

    def test_invalid_capability_value_raises(self, tmp_path: Path) -> None:
        path = _write_overlay(
            tmp_path / "models.toml",
            """
[[models]]
name = "one"
provider = "anthropic"

[models.capabilities]
prompt_cache = "forever"
""",
        )

        with pytest.raises(ValidationError):
            load_model_overlay([path])

    def test_later_file_overrides_earlier(self, tmp_path: Path) -> None:
        global_path = _write_overlay(
            tmp_path / "global.toml",
            """
[[models]]
name = "one"
provider = "anthropic"
alias = "shared"
context_window = 100000
""",
        )
        project_path = _write_overlay(
            tmp_path / "project.toml",
            """
[[models]]
name = "one"
provider = "anthropic"
alias = "shared"
context_window = 300000
""",
        )

        models = load_model_overlay([global_path, project_path])

        assert len(models) == 1
        assert models[0].context_window == 300_000


class TestMergeModelOverlays:
    def test_overlay_wins_by_alias(self) -> None:
        base = [
            ModelConfig(
                name="base", provider="mistral", alias="devstral-2", context_window=1
            )
        ]
        overlay = [
            ModelConfig(
                name="base", provider="mistral", alias="devstral-2", context_window=2
            )
        ]

        merged = merge_model_overlays(base, overlay)

        assert len(merged) == 1
        assert merged[0].context_window == 2

    def test_new_models_are_appended(self) -> None:
        base = [ModelConfig(name="base", provider="mistral", alias="base")]
        overlay = [
            ModelConfig(name="new", provider="mistral", alias="new"),
            ModelConfig(name="base", provider="mistral", alias="base", temperature=0.9),
        ]

        merged = merge_model_overlays(base, overlay)

        assert [model.alias for model in merged] == ["base", "new"]
        assert merged[0].temperature == 0.9


class TestValidateModels:
    def test_valid_models_return_no_errors(self) -> None:
        models = [ModelConfig(name="one", provider="anthropic", alias="one")]

        assert validate_models(models, [_provider()]) == []

    def test_duplicate_alias_is_reported(self) -> None:
        models = [
            ModelConfig(name="one", provider="anthropic", alias="dup"),
            ModelConfig(name="two", provider="anthropic", alias="dup"),
        ]

        errors = validate_models(models, [_provider()])

        assert any("Duplicate model alias" in error for error in errors)

    def test_unknown_provider_is_reported(self) -> None:
        models = [ModelConfig(name="one", provider="missing", alias="one")]

        errors = validate_models(models, [_provider()])

        assert any("unknown provider" in error for error in errors)

    def test_non_positive_context_window_is_reported(self) -> None:
        models = [
            ModelConfig(name="one", provider="anthropic", alias="one", context_window=0)
        ]

        errors = validate_models(models, [_provider()])

        assert any("context_window" in error for error in errors)
