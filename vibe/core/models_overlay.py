from __future__ import annotations

from pathlib import Path
import tomllib

from vibe.core.config import ModelConfig, ProviderConfig, get_vibe_home

MODELS_OVERLAY_FILENAME = "models.toml"
PROJECT_OVERLAY_DIR = ".qqcode"


def default_overlay_paths() -> list[Path]:
    """Return overlay files from lowest to highest precedence."""
    paths = [get_vibe_home() / MODELS_OVERLAY_FILENAME]
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        candidate = directory / PROJECT_OVERLAY_DIR / MODELS_OVERLAY_FILENAME
        if candidate.is_file():
            paths.append(candidate)
            break
    return paths


def load_model_overlay(paths: list[Path] | None = None) -> list[ModelConfig]:
    models: dict[str, ModelConfig] = {}
    for path in paths if paths is not None else default_overlay_paths():
        if not path.is_file():
            continue

        with path.open("rb") as handle:
            data = tomllib.load(handle)

        raw_models = data.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError(
                f"Invalid model overlay {path}: 'models' must be an array of tables"
            )

        seen_in_file: set[str] = set()
        for raw in raw_models:
            model = ModelConfig.model_validate(raw)
            if model.alias in seen_in_file:
                raise ValueError(f"Duplicate model alias '{model.alias}' in {path}")
            seen_in_file.add(model.alias)
            models[model.alias] = model

    return list(models.values())


def merge_model_overlays(
    base: list[ModelConfig], overlay: list[ModelConfig]
) -> list[ModelConfig]:
    merged = {model.alias: model for model in base}
    for model in overlay:
        merged[model.alias] = model
    return list(merged.values())


def validate_models(
    models: list[ModelConfig], providers: list[ProviderConfig]
) -> list[str]:
    errors: list[str] = []
    provider_names = {provider.name for provider in providers}
    seen_aliases: set[str] = set()

    for model in models:
        if model.alias in seen_aliases:
            errors.append(f"Duplicate model alias: '{model.alias}'")
        seen_aliases.add(model.alias)

        if model.provider not in provider_names:
            errors.append(
                f"Model '{model.alias}' references unknown provider '{model.provider}'"
            )
        if model.context_window <= 0:
            errors.append(f"Model '{model.alias}' has non-positive context_window")
        if model.max_output_tokens is not None and model.max_output_tokens <= 0:
            errors.append(f"Model '{model.alias}' has non-positive max_output_tokens")

    return errors
