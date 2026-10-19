from __future__ import annotations

from pydantic import ValidationError
import pytest

from vibe.core.config import (
    ModelCapabilities,
    ModelConfig,
    ThinkingLevel,
    clamp_thinking_level,
    resolve_thinking_level,
    supported_thinking_levels,
    thinking_effort,
)


def _model(**overrides: object) -> ModelConfig:
    fields: dict[str, object] = {
        "name": "model",
        "provider": "provider",
        "alias": "model",
        "capabilities": ModelCapabilities(reasoning=True),
    }
    fields.update(overrides)
    return ModelConfig(**fields)  # type: ignore[arg-type]


TABLED = {
    "thinking_levels": ["off", "low", "high", "xhigh"],
    "thinking_efforts": {"off": "none"},
}


def test_models_without_a_table_keep_the_legacy_levels() -> None:
    assert supported_thinking_levels(_model()) == (
        ThinkingLevel.OFF,
        ThinkingLevel.MINIMAL,
        ThinkingLevel.LOW,
        ThinkingLevel.MEDIUM,
        ThinkingLevel.HIGH,
    )


def test_legacy_models_clamp_new_levels_down_to_high() -> None:
    model = _model()

    assert resolve_thinking_level(model, ThinkingLevel.XHIGH) == ThinkingLevel.HIGH
    assert resolve_thinking_level(model, ThinkingLevel.MAX) == ThinkingLevel.HIGH
    assert resolve_thinking_level(model, ThinkingLevel.OFF) == ThinkingLevel.OFF
    assert resolve_thinking_level(model, None) is None


def test_non_reasoning_models_ignore_levels_but_keep_off() -> None:
    model = _model(capabilities=ModelCapabilities(reasoning=False))

    assert resolve_thinking_level(model, ThinkingLevel.HIGH) is None
    assert resolve_thinking_level(model, ThinkingLevel.OFF) == ThinkingLevel.OFF


def test_table_clamps_to_the_nearest_declared_level() -> None:
    model = _model(**TABLED)

    assert resolve_thinking_level(model, ThinkingLevel.MEDIUM) == ThinkingLevel.HIGH
    assert resolve_thinking_level(model, ThinkingLevel.MINIMAL) == ThinkingLevel.LOW
    assert resolve_thinking_level(model, ThinkingLevel.MAX) == ThinkingLevel.XHIGH
    assert resolve_thinking_level(model, ThinkingLevel.XHIGH) == ThinkingLevel.XHIGH


def test_off_becomes_the_lowest_level_when_the_model_cannot_disable_thinking() -> None:
    model = _model(thinking_levels=["low", "high"])

    assert resolve_thinking_level(model, ThinkingLevel.OFF) == ThinkingLevel.LOW


def test_configured_reasoning_level_is_clamped_like_an_override() -> None:
    model = _model(reasoning_level=ThinkingLevel.MEDIUM, **TABLED)

    assert resolve_thinking_level(model, None) == ThinkingLevel.HIGH


def test_supported_levels_are_ordered_from_off_to_max() -> None:
    model = _model(thinking_levels=["max", "off", "low"])

    assert supported_thinking_levels(model) == (
        ThinkingLevel.OFF,
        ThinkingLevel.LOW,
        ThinkingLevel.MAX,
    )


def test_clamp_prefers_a_higher_level() -> None:
    model = _model(thinking_levels=["low", "max"])

    assert clamp_thinking_level(model, ThinkingLevel.HIGH) == ThinkingLevel.MAX
    assert clamp_thinking_level(model, ThinkingLevel.MINIMAL) == ThinkingLevel.LOW


def test_effort_sends_mapped_values_and_nothing_for_unmapped_off() -> None:
    model = _model(**TABLED)

    assert thinking_effort(model, ThinkingLevel.OFF) == "none"
    assert thinking_effort(model, ThinkingLevel.HIGH) == "high"
    assert thinking_effort(model, None) is None


def test_off_without_a_mapping_sends_no_effort() -> None:
    model = _model(thinking_levels=["low", "high"])

    assert thinking_effort(model, ThinkingLevel.LOW) == "low"
    assert thinking_effort(model, ThinkingLevel.OFF) is None


def test_thinking_levels_require_reasoning() -> None:
    with pytest.raises(ValidationError, match="reasoning is off"):
        _model(capabilities=ModelCapabilities(reasoning=False), thinking_levels=["low"])


def test_efforts_must_name_declared_levels() -> None:
    with pytest.raises(ValidationError, match="maps levels it does not support"):
        _model(thinking_levels=["low"], thinking_efforts={"high": "high"})


def test_levels_cannot_repeat() -> None:
    with pytest.raises(ValidationError, match="twice"):
        _model(thinking_levels=["low", "low"])


def test_effort_values_cannot_be_blank() -> None:
    with pytest.raises(ValidationError, match="empty effort value"):
        _model(thinking_levels=["off"], thinking_efforts={"off": " "})
