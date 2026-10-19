from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vibe.core.config import Backend, ModelConfig, ProviderConfig


@dataclass(frozen=True)
class CachePlan:
    enabled: bool
    ttl: Literal["5m", "1h"] | None = None
    session_key: str | None = None


def _is_anthropic(provider: ProviderConfig, model: ModelConfig) -> bool:
    api_style = model.api_style or provider.api_style
    return provider.backend == Backend.ANTHROPIC or api_style == "anthropic"


def build_cache_plan(
    provider: ProviderConfig, model: ModelConfig, session_id: str
) -> CachePlan:
    """Resolve prompt-cache behavior for a request.

    Anthropic keeps its historical always-on cache_control behavior and models
    that declare prompt-cache support keep their explicit markers. Only an
    explicit provider ``cache_retention`` emits an OpenAI ``prompt_cache_key``
    so old providers are never surprised. A 1h TTL is only emitted for models
    that declare long prompt caching.
    """
    if provider.cache_retention == "none":
        return CachePlan(enabled=False)

    is_anthropic = _is_anthropic(provider, model)
    explicit_retention = provider.cache_retention in {"short", "long"}
    model_cache = model.capabilities.prompt_cache
    if not is_anthropic and not explicit_retention and model_cache == "none":
        return CachePlan(enabled=False)

    ttl: Literal["5m", "1h"] = "1h" if model_cache == "long" else "5m"
    session_key = session_id[:64] if explicit_retention and not is_anthropic else None
    return CachePlan(enabled=True, ttl=ttl, session_key=session_key)
