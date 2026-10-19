from __future__ import annotations

from typing import Any

from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter


def _parse(message: dict[str, Any]) -> Any:
    return OpenAIAdapter().parse_response({
        "choices": [{"message": message, "finish_reason": "stop"}]
    })


class TestOpenAIReasoningParsing:
    def test_reads_reasoning_content(self) -> None:
        chunk = _parse({
            "role": "assistant",
            "content": "hi",
            "reasoning_content": "thought",
        })

        assert chunk.message.reasoning_content == "thought"
        assert chunk.message.content == "hi"

    def test_falls_back_to_reasoning(self) -> None:
        chunk = _parse({"role": "assistant", "content": "hi", "reasoning": "thought"})

        assert chunk.message.reasoning_content == "thought"

    def test_falls_back_to_reasoning_text(self) -> None:
        chunk = _parse({
            "role": "assistant",
            "content": "hi",
            "reasoning_text": "thought",
        })

        assert chunk.message.reasoning_content == "thought"

    def test_prefers_reasoning_content(self) -> None:
        chunk = _parse({
            "role": "assistant",
            "content": "hi",
            "reasoning_content": "first",
            "reasoning": "second",
            "reasoning_text": "third",
        })

        assert chunk.message.reasoning_content == "first"

    def test_ignores_non_string_reasoning(self) -> None:
        chunk = _parse({
            "role": "assistant",
            "content": "hi",
            "reasoning": {"effort": "high"},
        })

        assert chunk.message.reasoning_content is None

    def test_streaming_delta_reasoning_content(self) -> None:
        chunk = OpenAIAdapter().parse_response({
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "Thinking about",
                    },
                    "finish_reason": None,
                }
            ]
        })

        assert chunk.message.reasoning_content == "Thinking about"

    def test_streaming_delta_reasoning_fallback(self) -> None:
        chunk = OpenAIAdapter().parse_response({
            "choices": [
                {
                    "delta": {"role": "assistant", "reasoning": "Thinking about"},
                    "finish_reason": None,
                }
            ]
        })

        assert chunk.message.reasoning_content == "Thinking about"
