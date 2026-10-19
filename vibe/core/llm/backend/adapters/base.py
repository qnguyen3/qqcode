from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Protocol, TypeVar

from vibe.core.config import ThinkingLevel

if TYPE_CHECKING:
    from vibe.core.config import ProviderConfig
    from vibe.core.llm.cache import CachePlan
    from vibe.core.types import AvailableTool, LLMChunk, LLMMessage, StrToolChoice


class PreparedRequest(NamedTuple):
    endpoint: str
    headers: dict[str, str]
    body: bytes


class UnknownAPIStyleError(ValueError):
    """Raised when no adapter is registered for a provider ``api_style``."""

    def __init__(self, api_style: str, available: list[str]) -> None:
        joined = ", ".join(available) if available else "<none>"
        super().__init__(
            f"Unknown api_style '{api_style}'. Registered adapters: {joined}."
        )
        self.api_style = api_style
        self.available = available


class APIAdapter(Protocol):
    endpoint: ClassVar[str]

    def prepare_request(
        self,
        *,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        enable_streaming: bool,
        provider: ProviderConfig,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
        reasoning_effort: str | None = None,
    ) -> PreparedRequest: ...

    def parse_response(self, data: dict[str, Any]) -> LLMChunk: ...

    def postprocess_response(self, chunk: LLMChunk) -> LLMChunk:
        """Hook for adapters that need to adjust every parsed chunk."""
        return chunk


def effective_reasoning_effort(
    thinking_level: ThinkingLevel | None, reasoning_effort: str | None
) -> str | None:
    """Return the effort value to send, or None to send no effort parameter.

    ``reasoning_effort`` is the provider value the backend mapped from the
    model's thinking table. Without it, the level name is sent and OFF sends
    nothing.
    """
    if reasoning_effort is not None:
        return reasoning_effort
    if thinking_level is None or thinking_level is ThinkingLevel.OFF:
        return None
    return thinking_level.value


ADAPTER_REGISTRY: dict[str, type[APIAdapter]] = {}
_ADAPTER_INSTANCES: dict[str, APIAdapter] = {}

T = TypeVar("T", bound=APIAdapter)


def register_adapter(name: str) -> Callable[[type[T]], type[T]]:
    """Register an adapter class under ``name`` for a provider ``api_style``."""

    def decorator(cls: type[T]) -> type[T]:
        ADAPTER_REGISTRY[name] = cls
        _ADAPTER_INSTANCES[name] = cls()
        return cls

    return decorator


def get_adapter(api_style: str) -> APIAdapter:
    """Return the adapter registered for ``api_style``.

    Raises:
        UnknownAPIStyleError: If no adapter is registered for the api style.
    """
    if (adapter := _ADAPTER_INSTANCES.get(api_style)) is not None:
        return adapter

    match ADAPTER_REGISTRY.get(api_style):
        case None:
            raise UnknownAPIStyleError(api_style, sorted(ADAPTER_REGISTRY))
        case adapter_cls:
            adapter = adapter_cls()
            _ADAPTER_INSTANCES[api_style] = adapter
            return adapter


BACKEND_ADAPTERS: dict[str, APIAdapter] = _ADAPTER_INSTANCES
