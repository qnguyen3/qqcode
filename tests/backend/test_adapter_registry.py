from __future__ import annotations

from typing import Any

import pytest

from vibe.core.llm.backend.adapters import (
    ADAPTER_REGISTRY,
    BACKEND_ADAPTERS,
    get_adapter,
    register_adapter,
)
from vibe.core.llm.backend.adapters.anthropic_messages import AnthropicAdapter
from vibe.core.llm.backend.adapters.base import (
    APIAdapter,
    PreparedRequest,
    UnknownAPIStyleError,
)
from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter
from vibe.core.llm.backend.adapters.openai_responses import OpenAIResponsesAdapter
from vibe.core.llm.backend.generic import (
    AnthropicAdapter as GenericAnthropicAdapter,
    OpenAIAdapter as GenericOpenAIAdapter,
)
from vibe.core.types import LLMChunk, LLMMessage, Role


def test_builtin_adapters_are_registered() -> None:
    assert set(ADAPTER_REGISTRY) >= {"openai", "anthropic", "openai-responses"}
    assert isinstance(get_adapter("openai"), OpenAIAdapter)
    assert isinstance(get_adapter("anthropic"), AnthropicAdapter)
    assert isinstance(get_adapter("openai-responses"), OpenAIResponsesAdapter)


def test_generic_module_still_re_exports_legacy_names() -> None:
    assert GenericOpenAIAdapter is OpenAIAdapter
    assert GenericAnthropicAdapter is AnthropicAdapter
    assert BACKEND_ADAPTERS["openai"] is get_adapter("openai")


def test_get_adapter_returns_the_same_instance() -> None:
    assert get_adapter("openai") is get_adapter("openai")


def test_unknown_api_style_raises_clear_error() -> None:
    with pytest.raises(UnknownAPIStyleError, match="unknown-style") as exc_info:
        get_adapter("unknown-style")

    assert "openai" in str(exc_info.value)


def test_register_custom_adapter() -> None:
    class DummyAdapter(APIAdapter):
        endpoint = "/dummy"
        calls = 0

        def prepare_request(self, **_: Any) -> PreparedRequest:
            return PreparedRequest(self.endpoint, {}, b"{}")

        def parse_response(self, data: dict[str, Any]) -> LLMChunk:
            self.calls += 1
            return LLMChunk(message=LLMMessage(role=Role.assistant, content="dummy"))

    decorated = register_adapter("dummy-test")(DummyAdapter)
    try:
        assert decorated is DummyAdapter
        adapter = get_adapter("dummy-test")
        assert isinstance(adapter, DummyAdapter)
        assert BACKEND_ADAPTERS["dummy-test"] is adapter
    finally:
        ADAPTER_REGISTRY.pop("dummy-test", None)
        BACKEND_ADAPTERS.pop("dummy-test", None)
