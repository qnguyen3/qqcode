from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig


def clamp_max_tokens(model: ModelConfig, max_tokens: int | None) -> int | None:
    """Cap a requested output budget at the model's declared maximum.

    Returns ``max_tokens`` unchanged when no request was made or the model
    does not declare ``max_output_tokens``.
    """
    if max_tokens is None or model.max_output_tokens is None:
        return max_tokens
    return min(max_tokens, model.max_output_tokens)
