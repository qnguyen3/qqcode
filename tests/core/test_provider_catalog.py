from __future__ import annotations

import pytest

from vibe.core.config import (
    DEFAULT_MODELS,
    DEFAULT_PROVIDERS,
    ModelConfig,
    ProviderConfig,
    VibeConfig,
)


def _providers_by_name(config: VibeConfig) -> dict[str, ProviderConfig]:
    return {provider.name: provider for provider in config.providers}


def test_default_provider_catalog_matches_expected_count() -> None:
    config = VibeConfig()

    assert len(config.providers) == len(DEFAULT_PROVIDERS) == 39
    assert len({provider.name for provider in config.providers}) == 39


def test_default_providers_load_with_expected_env_var_and_style() -> None:
    providers = _providers_by_name(VibeConfig())

    expected = {
        "openai": ("OPENAI_API_KEY", "openai-responses"),
        "openai-codex": ("", "openai-codex-responses"),
        "deepseek": ("DEEPSEEK_API_KEY", "openai"),
        "nvidia": ("NVIDIA_API_KEY", "openai"),
        "google": ("GEMINI_API_KEY", "google-generative-ai"),
        "github-copilot": ("COPILOT_GITHUB_TOKEN", "openai"),
        "groq": ("GROQ_API_KEY", "openai"),
        "cerebras": ("CEREBRAS_API_KEY", "openai"),
        "xai": ("XAI_API_KEY", "openai-responses"),
        "vercel-ai-gateway": ("AI_GATEWAY_API_KEY", "anthropic"),
        "zai-coding-cn": ("ZAI_CODING_CN_API_KEY", "openai"),
        "radius": ("RADIUS_API_KEY", "pi-messages"),
        "typesafe": ("TYPESAFE_API_KEY", "openai"),
        "huggingface": ("HF_TOKEN", "openai"),
        "fireworks": ("FIREWORKS_API_KEY", "openai"),
        "together": ("TOGETHER_API_KEY", "openai"),
        "baseten": ("BASETEN_API_KEY", "openai"),
        "kimi-coding": ("KIMI_API_KEY", "anthropic"),
        "meta": ("META_API_KEY", "openai-responses"),
        "minimax": ("MINIMAX_API_KEY", "anthropic"),
        "minimax-cn": ("MINIMAX_CN_API_KEY", "anthropic"),
        "moonshotai": ("MOONSHOT_API_KEY", "openai"),
        "moonshotai-cn": ("MOONSHOT_API_KEY", "openai"),
        "qwen-token-plan": ("QWEN_TOKEN_PLAN_API_KEY", "openai"),
        "qwen-token-plan-cn": ("QWEN_TOKEN_PLAN_CN_API_KEY", "openai"),
        "ant-ling": ("ANT_LING_API_KEY", "openai"),
        "opencode": ("OPENCODE_API_KEY", "openai"),
        "opencode-go": ("OPENCODE_API_KEY", "openai"),
    }

    for name, (env_var, api_style) in expected.items():
        provider = providers[name]
        assert provider.api_key_env_var == env_var, name
        assert provider.api_style == api_style, name


def test_static_provider_headers_are_declared() -> None:
    providers = _providers_by_name(VibeConfig())

    assert providers["nvidia"].extra_headers == {"NVCF-POLL-SECONDS": "3600"}
    assert providers["github-copilot"].extra_headers["Copilot-Integration-Id"] == (
        "vscode-chat"
    )


def test_default_models_reference_known_providers() -> None:
    config = VibeConfig()
    provider_names = {provider.name for provider in config.providers}

    assert all(model.provider in provider_names for model in DEFAULT_MODELS)
    assert len({model.alias for model in DEFAULT_MODELS}) == len(DEFAULT_MODELS)


def test_opencode_models_carry_a_per_model_api_style() -> None:
    models = {model.alias: model for model in VibeConfig().models}

    assert models["opencode/gpt-5.4"].api_style == "openai-responses"
    assert models["opencode/claude-sonnet-4.5"].api_style == "anthropic"
    assert models["opencode/kimi-k2.6"].api_style is None
    assert models["opencode-go/gpt-5.6-luna"].api_style == "openai-responses"
    assert models["github-copilot/claude-sonnet-4.5"].api_style == "anthropic"


def test_new_native_models_are_registered() -> None:
    config = VibeConfig()
    models = {model.alias: model for model in config.models}
    providers = _providers_by_name(config)

    assert models["google/gemini-3-flash"].provider == "google"
    assert models["google/gemini-2.5-pro"].provider == "google"
    assert models["openai-codex/gpt-5.1-codex"].provider == "openai-codex"
    assert providers["openai-codex"].api_style == "openai-codex-responses"


def test_legacy_model_without_api_style_still_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEGACY_API_KEY", "test-key")
    config = VibeConfig.model_validate({
        "active_model": "legacy",
        "providers": [
            {
                "name": "legacy-provider",
                "api_base": "https://legacy.example/v1",
                "api_key_env_var": "LEGACY_API_KEY",
            }
        ],
        "models": [
            {"name": "legacy-model", "provider": "legacy-provider", "alias": "legacy"}
        ],
    })

    model = config.get_active_model()
    assert model.api_style is None

    provider = config.get_provider_for_model(model)
    assert provider.api_style == "openai"
    assert ModelConfig(name="m", provider="p", alias="a").api_style is None
