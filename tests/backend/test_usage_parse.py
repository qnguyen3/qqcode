from __future__ import annotations

from tests.backend.data.fireworks import SIMPLE_CONVERSATION_PARAMS
from vibe.core.llm.backend.generic import AnthropicAdapter, OpenAIAdapter
from vibe.core.types import LLMUsage


class TestOpenAIUsageParsing:
    def test_parses_zero_cached_tokens_from_prompt_details(self) -> None:
        _, response, _ = SIMPLE_CONVERSATION_PARAMS[0]

        chunk = OpenAIAdapter().parse_response(response)

        assert chunk.usage == LLMUsage(
            prompt_tokens=100,
            completion_tokens=200,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )

    def test_parses_nonzero_cached_tokens(self) -> None:
        response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }

        chunk = OpenAIAdapter().parse_response(response)

        assert chunk.usage is not None
        assert chunk.usage.prompt_tokens == 100
        assert chunk.usage.cache_read_tokens == 80
        assert chunk.usage.cache_write_tokens == 0

    def test_parses_anthropic_compatible_cache_fields(self) -> None:
        response = {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 10,
            },
        }

        chunk = OpenAIAdapter().parse_response(response)

        assert chunk.usage is not None
        assert chunk.usage.cache_read_tokens == 30
        assert chunk.usage.cache_write_tokens == 10
        assert chunk.usage.prompt_tokens == 140


class TestAnthropicUsageParsing:
    def test_parses_cache_tokens_from_message(self) -> None:
        response = {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 10,
            },
        }

        chunk = AnthropicAdapter().parse_response(response)

        assert chunk.usage is not None
        assert chunk.usage.prompt_tokens == 140
        assert chunk.usage.completion_tokens == 20
        assert chunk.usage.cache_read_tokens == 30
        assert chunk.usage.cache_write_tokens == 10

    def test_missing_cache_fields_default_to_zero(self) -> None:
        response = {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }

        chunk = AnthropicAdapter().parse_response(response)

        assert chunk.usage == LLMUsage(
            prompt_tokens=5,
            completion_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
