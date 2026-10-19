#!/usr/bin/env python3
"""Regenerate the built-in model catalog.

``vibe/core/data/models.curated.toml`` lists every model qqcode ships. A
provider with an upstream catalog (models.dev, OpenRouter or the Vercel AI
Gateway) takes its metadata from that catalog, and the fields written in the
curated entry override it. A ``[[catalog]]`` rule adds every active,
tool-capable model that models.dev lists for its provider. The result is
written to ``vibe/core/data/models.generated.json``, the file qqcode loads at
startup.

Run from the repository root::

    uv run python scripts/generate_models.py            # update the snapshot
    uv run python scripts/generate_models.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any
import urllib.request

from pydantic import ValidationError

from vibe.core.config import DEFAULT_PROVIDERS, ModelConfig
from vibe.core.model_catalog import (
    CURATED_PATH,
    SNAPSHOT_PATH,
    SNAPSHOT_SCHEMA_VERSION,
    dump_model_snapshot,
    load_model_snapshot,
    models_digest,
)

MODELS_DEV_URL = "https://models.dev/api.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
VERCEL_GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/models"
REQUEST_TIMEOUT_SECONDS = 60
MILLION = 1_000_000

# qqcode provider -> models.dev provider key.
MODELS_DEV_PROVIDERS = {
    "amazon-bedrock": "amazon-bedrock",
    "anthropic": "anthropic",
    "baseten": "baseten",
    "cerebras": "cerebras",
    "cloudflare-ai-gateway": "cloudflare-ai-gateway",
    "cloudflare-workers-ai": "cloudflare-workers-ai",
    "deepseek": "deepseek",
    "fireworks": "fireworks-ai",
    "github-copilot": "github-copilot",
    "google": "google",
    "google-vertex": "google-vertex",
    "groq": "groq",
    "huggingface": "huggingface",
    "kimi-coding": "kimi-code-plan-global",
    "meta": "meta",
    "minimax": "minimax",
    "minimax-cn": "minimax-cn",
    "mistral": "mistral",
    "moonshotai": "moonshotai",
    "moonshotai-cn": "moonshotai-cn",
    "nvidia": "nvidia",
    "openai": "openai",
    "opencode": "opencode",
    "opencode-go": "opencode-go",
    "qwen-token-plan": "alibaba-token-plan",
    "qwen-token-plan-cn": "alibaba-token-plan-cn",
    "together": "togetherai",
    "xai": "xai",
    "xiaomimimo": "xiaomi",
    "zai": "zai-coding-plan",
    "zai-coding-cn": "zhipuai-coding-plan",
}

# Requests in these API styles send a reasoning effort, so they get an effort
# table. Other styles spend a token budget per level and need no table.
EFFORT_API_STYLES = frozenset({
    "openai",
    "openai-responses",
    "openai-codex-responses",
    "pi-messages",
})
EFFORT_VALUES = ("minimal", "low", "medium", "high", "xhigh", "max")

# models.dev names the AI SDK package that serves each model. These are the
# qqcode api_style values for the packages that differ from the provider default.
NPM_API_STYLES = {
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/google": "google-generative-ai",
    "@ai-sdk/openai": "openai-responses",
}
# OpenCode Go serves these models on chat completions, although models.dev
# names the Anthropic package for them.
OPENCODE_GO_CHAT_MODELS = frozenset({"qwen3.5-plus", "qwen3.6-plus"})
# Models that reject the temperature parameter. The Anthropic names follow pi's
# generator. The GPT-5.6 and GPT-6 names are reasoning models: gpt-5.6-luna and
# gpt-6-luna were observed rejecting it on OpenCode Go. Omitting temperature only
# falls back to the model default, but sending it to a model that rejects it fails.
TEMPERATURE_UNSUPPORTED = re.compile(
    r"claude-(?:opus-(?:4[.-][78]|5)|(?:sonnet|haiku)-5[.-]5)|gpt-(?:5\.6|6(?:\.1)?)-"
)


class GenerationError(Exception):
    """A curated entry or an upstream payload cannot produce a valid model."""


@dataclass(frozen=True)
class UpstreamModel:
    """Metadata an upstream catalog reports for one model."""

    tools: bool
    reasoning: bool
    vision: bool
    context_window: int | None
    max_output_tokens: int | None
    pricing: dict[str, float] | None
    efforts: frozenset[str] = frozenset()
    effort_off: bool = False


@dataclass
class Sources:
    """Raw upstream catalogs, indexed by provider key and model id."""

    models_dev: dict[str, dict[str, Any]] = field(default_factory=dict)
    openrouter: dict[str, dict[str, Any]] = field(default_factory=dict)
    vercel_gateway: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogRule:
    """Build every active, tool-capable model that models.dev lists for a provider."""

    provider: str
    exclude: frozenset[str] = frozenset()


def upstream_source(provider: str) -> str | None:
    """Return the upstream catalog that describes ``provider``, if any."""
    if provider in MODELS_DEV_PROVIDERS:
        return "models.dev"
    if provider == "openrouter":
        return "openrouter"
    if provider == "vercel-ai-gateway":
        return "vercel-ai-gateway"
    return None


def _rates(
    values: Mapping[str, Any] | None, keys: tuple[str, str, str, str], scale: float
) -> dict[str, float] | None:
    """Convert upstream prices to per-million pricing.

    Returns None when the input or output rate is missing. Missing cache rates
    count as zero, so cached tokens are never priced above the input rate.
    """
    if values is None or values.get(keys[0]) is None or values.get(keys[1]) is None:
        return None
    rates = [round(float(values.get(key) or 0) * scale, 6) for key in keys]
    return {
        "input_per_million": rates[0],
        "output_per_million": rates[1],
        "cache_read_per_million": rates[2],
        "cache_write_per_million": rates[3],
    }


def models_dev_model(raw: Mapping[str, Any]) -> UpstreamModel:
    """Normalize one models.dev model entry."""
    limit = raw.get("limit") or {}
    inputs = (raw.get("modalities") or {}).get("input") or []
    efforts = {
        value
        for option in raw.get("reasoning_options") or []
        if option.get("type") == "effort"
        for value in option.get("values") or []
        if isinstance(value, str)
    }
    return UpstreamModel(
        tools=raw.get("tool_call") is True,
        reasoning=raw.get("reasoning") is True,
        vision="image" in inputs,
        context_window=limit.get("context"),
        max_output_tokens=limit.get("output"),
        pricing=_rates(
            raw.get("cost"), ("input", "output", "cache_read", "cache_write"), 1.0
        ),
        efforts=frozenset(efforts),
        effort_off="none" in efforts,
    )


def openrouter_model(raw: Mapping[str, Any]) -> UpstreamModel:
    """Normalize one OpenRouter model entry."""
    parameters = set(raw.get("supported_parameters") or [])
    top_provider = raw.get("top_provider") or {}
    architecture = raw.get("architecture") or {}
    reasoning = raw.get("reasoning") or {}
    efforts = {
        value
        for value in reasoning.get("supported_efforts") or []
        if isinstance(value, str)
    }
    return UpstreamModel(
        tools="tools" in parameters,
        reasoning="reasoning" in parameters,
        vision="image" in (architecture.get("input_modalities") or []),
        context_window=top_provider.get("context_length") or raw.get("context_length"),
        max_output_tokens=top_provider.get("max_completion_tokens"),
        pricing=_rates(
            raw.get("pricing"),
            ("prompt", "completion", "input_cache_read", "input_cache_write"),
            MILLION,
        ),
        efforts=frozenset(efforts),
        # OpenRouter accepts "none" for every model that is not mandatory.
        effort_off=reasoning.get("mandatory") is not True,
    )


def vercel_model(raw: Mapping[str, Any]) -> UpstreamModel:
    """Normalize one Vercel AI Gateway model entry."""
    tags = set(raw.get("tags") or [])
    return UpstreamModel(
        tools="tool-use" in tags,
        reasoning="reasoning" in tags,
        vision="vision" in tags,
        context_window=raw.get("context_window"),
        max_output_tokens=raw.get("max_tokens"),
        pricing=_rates(
            raw.get("pricing"),
            ("input", "output", "input_cache_read", "input_cache_write"),
            MILLION,
        ),
    )


def find_upstream(provider: str, name: str, sources: Sources) -> UpstreamModel | None:
    """Return the upstream metadata for ``name`` under ``provider``."""
    match upstream_source(provider):
        case "models.dev":
            raw = sources.models_dev.get(MODELS_DEV_PROVIDERS[provider], {}).get(name)
            return models_dev_model(raw) if raw else None
        case "openrouter":
            raw = sources.openrouter.get(name)
            return openrouter_model(raw) if raw else None
        case "vercel-ai-gateway":
            raw = sources.vercel_gateway.get(name)
            return vercel_model(raw) if raw else None
        case _:
            return None


def thinking_table(upstream: UpstreamModel) -> tuple[list[str], dict[str, str]]:
    """Return ``(thinking_levels, thinking_efforts)`` for a reasoning model.

    Off is included only when the upstream accepts an effort of "none", and it
    is sent as that value. Levels the upstream does not name are left out, so a
    request for one of them is clamped to the nearest level the model accepts.
    """
    levels = [value for value in EFFORT_VALUES if value in upstream.efforts]
    if not levels:
        return [], {}
    if upstream.effort_off:
        return ["off", *levels], {"off": "none"}
    return levels, {}


def upstream_fields(upstream: UpstreamModel, api_style: str) -> dict[str, Any]:
    """Return the ModelConfig fields the upstream catalog provides."""
    fields: dict[str, Any] = {
        "capabilities": {"reasoning": upstream.reasoning, "vision": upstream.vision}
    }
    if upstream.context_window is not None:
        fields["context_window"] = upstream.context_window
    if upstream.max_output_tokens is not None:
        fields["max_output_tokens"] = upstream.max_output_tokens
    if upstream.pricing is not None:
        fields["pricing"] = upstream.pricing
    if upstream.reasoning and api_style in EFFORT_API_STYLES:
        levels, efforts = thinking_table(upstream)
        if levels:
            fields["thinking_levels"] = levels
        if efforts:
            fields["thinking_efforts"] = efforts
    return fields


def build_model(
    entry: Mapping[str, Any], upstream: UpstreamModel | None, api_style: str
) -> dict[str, Any]:
    """Merge upstream metadata with a curated entry and validate the result.

    The curated entry wins field by field. ``capabilities`` and ``pricing`` are
    merged key by key. Context window and pricing have no defaults: a model
    without them fails, because a silent default would be wrong for a real
    model.
    """
    label = f"{entry['provider']}/{entry['alias']}"
    model: dict[str, Any] = {}
    if upstream is not None:
        if not upstream.tools:
            raise GenerationError(
                f"{label}: upstream model {entry['name']!r} does not support tools"
            )
        model.update(upstream_fields(upstream, api_style))
    if TEMPERATURE_UNSUPPORTED.search(entry["name"]):
        model["capabilities"] = {
            **model.get("capabilities", {}),
            "accepts_temperature": False,
        }
    for key, value in entry.items():
        if key == "manual":
            continue
        if key in {"capabilities", "pricing"} and isinstance(value, Mapping):
            model[key] = {**model.get(key, {}), **value}
        else:
            model[key] = value

    missing = [key for key in ("context_window", "pricing") if key not in model]
    if missing:
        raise GenerationError(
            f"{label}: {' and '.join(missing)} is neither upstream nor curated"
        )
    try:
        validated = ModelConfig.model_validate(model)
    except ValidationError as exc:
        raise GenerationError(f"{label}: {exc}") from exc
    return validated.model_dump(mode="json", exclude_defaults=True)


def load_curated(path: Path) -> list[dict[str, Any]]:
    """Read and check the curated model entries."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("models")
    if not isinstance(entries, list) or not entries:
        raise GenerationError(f"{path} must define at least one [[models]] entry")
    providers = {provider.name for provider in DEFAULT_PROVIDERS}
    aliases: set[str] = set()
    for entry in entries:
        for key in ("provider", "name", "alias"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise GenerationError(f"{path}: every [[models]] entry needs {key}")
        if entry["provider"] not in providers:
            raise GenerationError(
                f"{path}: unknown provider {entry['provider']!r} "
                f"for alias {entry['alias']!r}"
            )
        if entry["alias"] in aliases:
            raise GenerationError(f"{path}: duplicate alias {entry['alias']!r}")
        aliases.add(entry["alias"])
    return entries


def load_catalog_rules(path: Path) -> list[CatalogRule]:
    """Read the [[catalog]] rules that build whole providers from models.dev."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    providers = {provider.name for provider in DEFAULT_PROVIDERS}
    rules: list[CatalogRule] = []
    for entry in data.get("catalog", []):
        provider = entry.get("provider")
        if provider not in providers:
            raise GenerationError(
                f"{path}: [[catalog]] has unknown provider {provider!r}"
            )
        if provider not in MODELS_DEV_PROVIDERS:
            raise GenerationError(
                f"{path}: [[catalog]] provider {provider!r} has no models.dev catalog"
            )
        if any(rule.provider == provider for rule in rules):
            raise GenerationError(
                f"{path}: {provider!r} has more than one [[catalog]] rule"
            )
        exclude = entry.get("exclude", [])
        if not isinstance(exclude, list) or not all(
            isinstance(name, str) for name in exclude
        ):
            raise GenerationError(
                f"{path}: exclude for {provider!r} must be a list of model names"
            )
        rules.append(CatalogRule(provider, frozenset(exclude)))
    return rules


def build_models(
    curated: list[dict[str, Any]], sources: Sources
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build every curated model, collecting all errors instead of stopping."""
    providers = {provider.name: provider for provider in DEFAULT_PROVIDERS}
    models: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in curated:
        label = f"{entry['provider']}/{entry['alias']}"
        provider = providers[entry["provider"]]
        api_style = entry.get("api_style") or provider.api_style
        upstream = None
        source = upstream_source(entry["provider"])
        if source is not None and entry.get("manual") is not True:
            upstream = find_upstream(entry["provider"], entry["name"], sources)
            if upstream is None:
                errors.append(
                    f"{label}: {entry['name']!r} is not in the {source} catalog; "
                    "fix the name or mark the entry manual = true"
                )
                continue
        try:
            models.append(build_model(entry, upstream, api_style))
        except GenerationError as exc:
            errors.append(str(exc))
    return models, errors


def catalog_api_style(
    provider: str, model_id: str, raw: Mapping[str, Any]
) -> str | None:
    """Return the api_style a models.dev model implies, or None for the default."""
    if provider == "opencode-go" and model_id in OPENCODE_GO_CHAT_MODELS:
        return "openai"
    package = (raw.get("provider") or {}).get("npm") or ""
    return NPM_API_STYLES.get(package)


def catalog_entries(
    rules: Sequence[CatalogRule], curated: list[dict[str, Any]], sources: Sources
) -> tuple[list[dict[str, Any]], list[str]]:
    """Expand each catalog rule into curated-style entries.

    A model the curated list already names keeps its curated entry. A rule that
    finds no active tool-capable model is an error, because an empty provider
    would otherwise disappear without notice.
    """
    listed = {(entry["provider"], entry["name"]) for entry in curated}
    aliases = {entry["alias"] for entry in curated}
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for rule in rules:
        active = 0
        upstream = sources.models_dev.get(MODELS_DEV_PROVIDERS[rule.provider], {})
        for model_id, raw in upstream.items():
            if raw.get("tool_call") is not True or raw.get("status") == "deprecated":
                continue
            active += 1
            if model_id in rule.exclude or (rule.provider, model_id) in listed:
                continue
            alias = f"{rule.provider}/{model_id}"
            if alias in aliases:
                errors.append(f"{alias}: alias is already used by another model")
                continue
            aliases.add(alias)
            entry: dict[str, Any] = {
                "provider": rule.provider,
                "name": model_id,
                "alias": alias,
            }
            api_style = catalog_api_style(rule.provider, model_id, raw)
            if api_style is not None:
                entry["api_style"] = api_style
            entries.append(entry)
        if active == 0:
            errors.append(
                f"{rule.provider}: catalog rule found no active tool-capable "
                "models in models.dev"
            )
    return entries, errors


def build_catalog(
    curated: list[dict[str, Any]], rules: Sequence[CatalogRule], sources: Sources
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the curated entries together with the entries each rule generates."""
    generated, errors = catalog_entries(rules, curated, sources)
    models, build_errors = build_models([*curated, *generated], sources)
    return models, [*errors, *build_errors]


def upstream_candidates(
    sources: Sources, curated: list[dict[str, Any]], rules: Sequence[CatalogRule] = ()
) -> list[tuple[str, str, str]]:
    """Return tool-capable upstream models that the curated list omits.

    Providers with a catalog rule are skipped, because the rule builds them.
    """
    listed = {(entry["provider"], entry["name"]) for entry in curated}
    covered = {rule.provider for rule in rules}
    found: list[tuple[str, str, str]] = []
    for provider, key in MODELS_DEV_PROVIDERS.items():
        if provider in covered:
            continue
        for model_id, raw in sources.models_dev.get(key, {}).items():
            if (
                raw.get("tool_call") is True
                and raw.get("status") != "deprecated"
                and (provider, model_id) not in listed
            ):
                found.append((provider, model_id, raw.get("name") or model_id))
    for provider, catalog in (
        ("openrouter", sources.openrouter),
        ("vercel-ai-gateway", sources.vercel_gateway),
    ):
        for model_id, raw in catalog.items():
            if (provider, model_id) in listed:
                continue
            if provider == "openrouter":
                tools = "tools" in (raw.get("supported_parameters") or [])
            else:
                tools = "tool-use" in (raw.get("tags") or [])
            if tools:
                found.append((provider, model_id, raw.get("name") or model_id))
    return sorted(found)


def describe_changes(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[str]:
    """Describe added, removed and changed aliases between two snapshots."""
    before = {model["alias"]: model for model in previous}
    after = {model["alias"]: model for model in current}
    lines = [f"added {alias}" for alias in sorted(after.keys() - before.keys())]
    lines += [f"removed {alias}" for alias in sorted(before.keys() - after.keys())]
    for alias in sorted(before.keys() & after.keys()):
        fields = sorted(
            key
            for key in before[alias].keys() | after[alias].keys()
            if before[alias].get(key) != after[alias].get(key)
        )
        if fields:
            lines.append(f"changed {alias}: {', '.join(fields)}")
    return lines


def render_report(changes: list[str], candidates: list[tuple[str, str, str]]) -> str:
    """Render the run as Markdown for a pull request body."""
    lines = ["## Model catalog changes", ""]
    if changes:
        lines += [f"- {change}" for change in changes]
    else:
        lines.append("No changes to the built-in catalog.")
    lines += ["", "## Upstream models not in models.curated.toml", ""]
    if candidates:
        lines += [
            f"- `{provider}`: `{model_id}` ({name})"
            for provider, model_id, name in candidates
        ]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def fetch_json(url: str) -> Any:
    """Download and decode one upstream JSON document."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "qqcode-generate-models"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.load(response)


def fetch_sources() -> Sources:
    """Download the upstream catalogs. Any failure aborts the run."""
    models_dev = fetch_json(MODELS_DEV_URL)
    openrouter = fetch_json(OPENROUTER_URL)
    gateway = fetch_json(VERCEL_GATEWAY_URL)
    return Sources(
        models_dev={
            key: dict(value.get("models") or {})
            for key, value in models_dev.items()
            if isinstance(value, dict)
        },
        openrouter={item["id"]: item for item in openrouter["data"]},
        vercel_gateway={item["id"]: item for item in gateway["data"]},
    )


def main(argv: list[str] | None = None) -> int:
    """Regenerate the snapshot, or report what would change with --dry-run."""
    parser = argparse.ArgumentParser(
        description="Regenerate the built-in model catalog from curated sources."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report changes without writing the snapshot",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="also write a Markdown summary of the run to this path",
    )
    args = parser.parse_args(argv)

    try:
        curated = load_curated(CURATED_PATH)
        rules = load_catalog_rules(CURATED_PATH)
        needs_upstream = bool(rules) or any(
            upstream_source(entry["provider"]) is not None
            and entry.get("manual") is not True
            for entry in curated
        )
        sources = fetch_sources() if needs_upstream else Sources()
        models, errors = build_catalog(curated, rules, sources)
    except (GenerationError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    models.sort(key=lambda model: (model["provider"], model["alias"]))
    previous = load_model_snapshot() if SNAPSHOT_PATH.exists() else {}
    changes = describe_changes(previous.get("models", []), models)
    candidates = upstream_candidates(sources, curated, rules)
    digest = models_digest(models)
    unchanged = previous.get("models_sha256") == digest

    if args.report:
        args.report.write_text(render_report(changes, candidates), encoding="utf-8")
    counts = Counter(model["provider"] for model in models)
    for provider in sorted(counts):
        print(f"  {provider}: {counts[provider]} models")
    print(f"{len(changes)} change(s) against the committed snapshot:")
    for change in changes:
        print(f"  {change}")
    print(f"{len(candidates)} upstream model(s) not in models.curated.toml")

    if unchanged:
        print("Snapshot unchanged; nothing written.")
        return 0
    if args.dry_run:
        print("Dry run: snapshot not written.")
        return 0
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "models_sha256": digest,
        "sources": {
            "models.dev": MODELS_DEV_URL,
            "openrouter": OPENROUTER_URL,
            "vercel-ai-gateway": VERCEL_GATEWAY_URL,
        },
        "models": models,
    }
    SNAPSHOT_PATH.write_text(dump_model_snapshot(payload), encoding="utf-8")
    print(f"Wrote {len(models)} models to {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
