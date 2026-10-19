from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

from vibe.core.model_catalog import CURATED_PATH

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_models.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_models", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _upstream(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "tools": True,
        "reasoning": False,
        "vision": False,
        "context_window": 200_000,
        "max_output_tokens": 8_000,
        "pricing": {
            "input_per_million": 1.0,
            "output_per_million": 2.0,
            "cache_read_per_million": 0.0,
            "cache_write_per_million": 0.0,
        },
    }
    fields.update(overrides)
    return gen.UpstreamModel(**fields)


def test_models_dev_costs_are_per_million() -> None:
    model = gen.models_dev_model({
        "tool_call": True,
        "limit": {"context": 400_000, "output": 128_000},
        "modalities": {"input": ["text", "image"]},
        "cost": {"input": 2.5, "output": 15, "cache_read": 0.25, "cache_write": 0},
    })

    assert model.pricing == {
        "input_per_million": 2.5,
        "output_per_million": 15.0,
        "cache_read_per_million": 0.25,
        "cache_write_per_million": 0.0,
    }
    assert model.vision is True
    assert model.context_window == 400_000


def test_models_dev_model_without_cost_has_no_pricing() -> None:
    model = gen.models_dev_model({"tool_call": True, "limit": {"context": 1000}})

    assert model.pricing is None


def test_openrouter_per_token_prices_scale_to_per_million() -> None:
    model = gen.openrouter_model({
        "id": "openai/gpt-5.2",
        "supported_parameters": ["tools", "reasoning"],
        "pricing": {
            "prompt": "0.0000015",
            "completion": "0.000014",
            "input_cache_read": "0.00000015",
        },
        "top_provider": {"context_length": 400_000, "max_completion_tokens": 128_000},
    })

    assert model.tools is True
    assert model.reasoning is True
    assert model.pricing is not None
    assert model.pricing["input_per_million"] == 1.5
    assert model.pricing["output_per_million"] == 14.0
    assert model.pricing["cache_read_per_million"] == 0.15
    assert model.pricing["cache_write_per_million"] == 0.0


def test_thinking_table_orders_levels_and_maps_none_to_off() -> None:
    upstream = _upstream(
        reasoning=True, efforts=frozenset({"xhigh", "low", "none"}), effort_off=True
    )

    assert gen.thinking_table(upstream) == (["off", "low", "xhigh"], {"off": "none"})


def test_thinking_table_without_none_leaves_off_out() -> None:
    upstream = _upstream(reasoning=True, efforts=frozenset({"high"}))

    assert gen.thinking_table(upstream) == (["high"], {})


def test_upstream_without_effort_values_gets_no_table() -> None:
    assert gen.thinking_table(_upstream(reasoning=True)) == ([], {})


def test_effort_table_only_for_effort_api_styles() -> None:
    upstream = _upstream(reasoning=True, efforts=frozenset({"high"}))

    assert "thinking_levels" in gen.upstream_fields(upstream, "openai-responses")
    assert "thinking_levels" not in gen.upstream_fields(upstream, "anthropic")


def test_curated_fields_override_upstream_and_merge_capabilities() -> None:
    entry = {
        "provider": "anthropic",
        "name": "claude-x",
        "alias": "claude-x",
        "capabilities": {"prompt_cache": "short"},
        "context_window": 150_000,
    }

    model = gen.build_model(entry, _upstream(reasoning=True), "anthropic")

    assert model["context_window"] == 150_000
    assert model["capabilities"] == {"reasoning": True, "prompt_cache": "short"}


def test_context_window_and_pricing_have_no_silent_default() -> None:
    entry = {"provider": "deepseek", "name": "m", "alias": "m", "manual": True}

    with pytest.raises(gen.GenerationError, match="context_window and pricing"):
        gen.build_model(entry, None, "openai")


def test_upstream_model_without_tools_is_rejected() -> None:
    entry = {"provider": "openai", "name": "m", "alias": "m"}

    with pytest.raises(gen.GenerationError, match="does not support tools"):
        gen.build_model(entry, _upstream(tools=False), "openai-responses")


def test_describe_changes_reports_added_removed_and_changed() -> None:
    before = [{"alias": "a", "context_window": 1}, {"alias": "gone"}]
    after = [{"alias": "a", "context_window": 2}, {"alias": "new"}]

    assert gen.describe_changes(before, after) == [
        "added new",
        "removed gone",
        "changed a: context_window",
    ]


def test_upstream_candidates_skip_listed_deprecated_and_toolless_models() -> None:
    sources = gen.Sources(
        models_dev={
            "anthropic": {
                "claude-listed": {"tool_call": True, "name": "Listed"},
                "claude-new": {"tool_call": True, "name": "New"},
                "claude-old": {"tool_call": True, "status": "deprecated"},
                "claude-chat": {"tool_call": False},
            }
        }
    )
    curated = [{"provider": "anthropic", "name": "claude-listed", "alias": "a"}]

    assert gen.upstream_candidates(sources, curated) == [
        ("anthropic", "claude-new", "New")
    ]


def test_committed_curated_file_loads() -> None:
    entries = gen.load_curated(CURATED_PATH)

    assert entries
    assert all(entry["alias"] for entry in entries)


def test_curated_aliases_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "curated.toml"
    path.write_text(
        '[[models]]\nprovider = "anthropic"\nname = "a"\nalias = "x"\n'
        '[[models]]\nprovider = "anthropic"\nname = "b"\nalias = "x"\n',
        encoding="utf-8",
    )

    with pytest.raises(gen.GenerationError, match="duplicate alias"):
        gen.load_curated(path)


def test_curated_provider_must_be_known(tmp_path: Path) -> None:
    path = tmp_path / "curated.toml"
    path.write_text(
        '[[models]]\nprovider = "nope"\nname = "a"\nalias = "x"\n', encoding="utf-8"
    )

    with pytest.raises(gen.GenerationError, match="unknown provider"):
        gen.load_curated(path)


@pytest.mark.parametrize("name", ["claude-haiku-5-5", "gpt-5.6-luna", "gpt-6-luna"])
def test_models_that_reject_temperature_are_marked(name: str) -> None:
    entry = {
        "provider": "opencode-go",
        "name": name,
        "alias": f"opencode-go/{name}",
        "context_window": 200_000,
        "pricing": {"input_per_million": 1.0, "output_per_million": 2.0},
    }

    model = gen.build_model(entry, None, "anthropic")

    assert model["capabilities"] == {"accepts_temperature": False}


def test_models_that_accept_temperature_keep_the_default() -> None:
    entry = {
        "provider": "opencode-go",
        "name": "kimi-k3",
        "alias": "opencode-go/kimi-k3",
        "context_window": 200_000,
        "pricing": {"input_per_million": 1.0, "output_per_million": 2.0},
    }

    model = gen.build_model(entry, None, "openai")

    assert "capabilities" not in model


def test_catalog_rule_builds_active_tool_models_and_skips_listed_ones() -> None:
    sources = gen.Sources(
        models_dev={
            "deepseek": {
                "deepseek-flash": {"tool_call": True, "name": "Flash"},
                "deepseek-v4-pro": {"tool_call": True, "name": "Pro"},
                "deepseek-old": {"tool_call": True, "status": "deprecated"},
                "deepseek-chat": {"tool_call": False},
            }
        }
    )
    curated = [
        {
            "provider": "deepseek",
            "name": "deepseek-flash",
            "alias": "deepseek/deepseek-flash",
        }
    ]

    entries, errors = gen.catalog_entries(
        [gen.CatalogRule("deepseek")], curated, sources
    )

    assert errors == []
    assert entries == [
        {
            "provider": "deepseek",
            "name": "deepseek-v4-pro",
            "alias": "deepseek/deepseek-v4-pro",
        }
    ]


def test_catalog_rule_exclude_drops_named_models() -> None:
    sources = gen.Sources(
        models_dev={
            "opencode": {
                "gpt-5.3-codex-spark": {"tool_call": True},
                "kimi-k3": {"tool_call": True},
            }
        }
    )
    rules = [gen.CatalogRule("opencode", frozenset({"gpt-5.3-codex-spark"}))]

    entries, _ = gen.catalog_entries(rules, [], sources)

    assert [entry["name"] for entry in entries] == ["kimi-k3"]


def test_catalog_rule_takes_api_style_from_the_models_dev_package() -> None:
    sources = gen.Sources(
        models_dev={
            "opencode": {
                "claude-x": {
                    "tool_call": True,
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
                "gpt-x": {"tool_call": True, "provider": {"npm": "@ai-sdk/openai"}},
                "plain": {"tool_call": True},
            },
            "opencode-go": {
                "qwen3.6-plus": {
                    "tool_call": True,
                    "provider": {"npm": "@ai-sdk/anthropic"},
                }
            },
        }
    )
    rules = [gen.CatalogRule("opencode"), gen.CatalogRule("opencode-go")]

    entries, errors = gen.catalog_entries(rules, [], sources)

    assert errors == []
    styles = {entry["name"]: entry.get("api_style", "openai") for entry in entries}
    assert styles == {
        "claude-x": "anthropic",
        "gpt-x": "openai-responses",
        "plain": "openai",
        "qwen3.6-plus": "openai",
    }


def test_catalog_rule_without_active_models_is_an_error() -> None:
    sources = gen.Sources(
        models_dev={"deepseek": {"old": {"tool_call": True, "status": "deprecated"}}}
    )

    entries, errors = gen.catalog_entries([gen.CatalogRule("deepseek")], [], sources)

    assert entries == []
    assert errors == [
        "deepseek: catalog rule found no active tool-capable models in models.dev"
    ]


def test_catalog_rule_alias_already_in_use_is_an_error() -> None:
    sources = gen.Sources(models_dev={"deepseek": {"m": {"tool_call": True}}})
    curated = [{"provider": "anthropic", "name": "other", "alias": "deepseek/m"}]

    _, errors = gen.catalog_entries([gen.CatalogRule("deepseek")], curated, sources)

    assert errors == ["deepseek/m: alias is already used by another model"]


def test_catalog_rule_provider_needs_a_models_dev_catalog(tmp_path: Path) -> None:
    path = tmp_path / "curated.toml"
    path.write_text('[[catalog]]\nprovider = "openrouter"\n', encoding="utf-8")

    with pytest.raises(gen.GenerationError, match="has no models.dev catalog"):
        gen.load_catalog_rules(path)


def test_committed_catalog_rules_cover_mimo_deepseek_and_opencode() -> None:
    rules = gen.load_catalog_rules(CURATED_PATH)

    assert {rule.provider for rule in rules} == {
        "xiaomimimo",
        "deepseek",
        "opencode",
        "opencode-go",
    }


def test_catalog_providers_are_left_out_of_candidates() -> None:
    sources = gen.Sources(
        models_dev={"deepseek": {"new-one": {"tool_call": True, "name": "New"}}}
    )

    assert gen.upstream_candidates(sources, [], [gen.CatalogRule("deepseek")]) == []
    assert gen.upstream_candidates(sources, []) == [("deepseek", "new-one", "New")]
