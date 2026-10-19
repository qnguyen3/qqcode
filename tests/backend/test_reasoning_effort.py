from __future__ import annotations

import json
from typing import Any

from vibe.core.config import (
    ModelCapabilities,
    ModelConfig,
    ProviderConfig,
    ThinkingLevel,
)
from vibe.core.llm.backend.adapters.base import effective_reasoning_effort
from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter
from vibe.core.llm.backend.adapters.openai_responses import OpenAIResponsesAdapter
from vibe.core.llm.backend.adapters.pi_messages import PiMessagesAdapter
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import LLMMessage, Role

MESSAGES = [LLMMessage(role=Role.user, content="hi")]
PROVIDER = ProviderConfig(name="provider", api_base="https://example.com")


def _chat_body(**kwargs: Any) -> dict[str, Any]:
    request = OpenAIAdapter().prepare_request(
        model_name="model",
        messages=MESSAGES,
        temperature=0.4,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=PROVIDER,
        api_key="key",
        **kwargs,
    )
    return json.loads(request.body)


def test_mapped_effort_wins_over_the_level_name() -> None:
    assert effective_reasoning_effort(ThinkingLevel.HIGH, "xhigh") == "xhigh"
    assert effective_reasoning_effort(ThinkingLevel.OFF, "none") == "none"


def test_without_a_mapping_the_level_name_is_sent_and_off_sends_nothing() -> None:
    assert effective_reasoning_effort(ThinkingLevel.HIGH, None) == "high"
    assert effective_reasoning_effort(ThinkingLevel.OFF, None) is None
    assert effective_reasoning_effort(None, None) is None


def test_chat_sends_the_mapped_effort() -> None:
    body = _chat_body(thinking_level=ThinkingLevel.OFF, reasoning_effort="none")

    assert body["reasoning_effort"] == "none"


def test_chat_without_a_mapping_keeps_the_legacy_payload() -> None:
    assert _chat_body(thinking_level=ThinkingLevel.LOW)["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _chat_body(thinking_level=ThinkingLevel.OFF)


def test_extra_body_reasoning_still_wins_over_the_mapped_effort() -> None:
    body = _chat_body(
        thinking_level=ThinkingLevel.HIGH,
        reasoning_effort="high",
        extra_body={"reasoning": {"effort": "none"}},
    )

    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "none"}


def test_responses_sends_the_mapped_effort() -> None:
    request = OpenAIResponsesAdapter().prepare_request(
        model_name="model",
        messages=MESSAGES,
        temperature=0.4,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=PROVIDER,
        api_key="key",
        thinking_level=ThinkingLevel.OFF,
        reasoning_effort="none",
    )

    assert json.loads(request.body)["reasoning"] == {"effort": "none"}


def test_pi_messages_sends_the_mapped_effort() -> None:
    request = PiMessagesAdapter().prepare_request(
        model_name="model",
        messages=MESSAGES,
        temperature=0.4,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=True,
        provider=PROVIDER,
        api_key="key",
        thinking_level=ThinkingLevel.XHIGH,
        reasoning_effort="xhigh",
    )

    assert json.loads(request.body)["options"]["reasoning"] == "xhigh"


def test_backend_maps_the_clamped_level_to_its_effort() -> None:
    model = ModelConfig(
        name="model",
        provider="provider",
        alias="model",
        capabilities=ModelCapabilities(reasoning=True),
        thinking_levels=[
            ThinkingLevel.OFF,
            ThinkingLevel.LOW,
            ThinkingLevel.HIGH,
            ThinkingLevel.XHIGH,
        ],
        thinking_efforts={ThinkingLevel.OFF: "none"},
    )
    backend = GenericBackend(provider=PROVIDER)

    assert backend._thinking_request(model, ThinkingLevel.OFF) == {
        "thinking_level": ThinkingLevel.OFF,
        "reasoning_effort": "none",
    }
    assert backend._thinking_request(model, ThinkingLevel.MEDIUM) == {
        "thinking_level": ThinkingLevel.HIGH,
        "reasoning_effort": "high",
    }
