from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from vibe.cli.entrypoint import _build_models_parser
from vibe.core.config import DEFAULT_MODELS
from vibe.core.model_catalog import (
    SNAPSHOT_MAX_AGE,
    SNAPSHOT_SCHEMA_VERSION,
    load_model_snapshot,
    models_digest,
    snapshot_problems,
)

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def _snapshot(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": "2026-10-19T00:00:00+00:00",
        "models_sha256": models_digest(models),
        "sources": {},
        "models": models,
    }


def test_committed_snapshot_passes_integrity_checks() -> None:
    errors, _ = snapshot_problems(load_model_snapshot(), now=datetime.now(UTC))

    assert errors == []


def test_built_in_models_come_from_the_snapshot() -> None:
    assert len(DEFAULT_MODELS) == len(load_model_snapshot()["models"])


def test_digest_ignores_key_order() -> None:
    assert models_digest([{"a": 1, "b": 2}]) == models_digest([{"b": 2, "a": 1}])


def test_hand_edited_models_fail_the_digest() -> None:
    payload = _snapshot([{"provider": "p", "name": "m", "alias": "a"}])
    payload["models"][0]["alias"] = "b"

    errors, _ = snapshot_problems(payload, now=NOW)

    assert any("models_sha256" in error for error in errors)


def test_old_snapshot_only_warns() -> None:
    payload = _snapshot([])
    payload["generated_at"] = (NOW - SNAPSHOT_MAX_AGE - timedelta(days=1)).isoformat()

    errors, warnings = snapshot_problems(payload, now=NOW)

    assert errors == []
    assert len(warnings) == 1


def test_timestamp_without_timezone_is_an_error() -> None:
    payload = _snapshot([])
    payload["generated_at"] = "2026-10-19T00:00:00"

    errors, _ = snapshot_problems(payload, now=NOW)

    assert errors == ["generated_at must include a timezone offset"]


def test_unknown_schema_version_is_an_error() -> None:
    payload = _snapshot([])
    payload["schema_version"] = SNAPSHOT_SCHEMA_VERSION + 1

    errors, _ = snapshot_problems(payload, now=NOW)

    assert any("schema_version" in error for error in errors)


def test_models_validate_accepts_strict_flag() -> None:
    assert _build_models_parser().parse_args(["validate"]).strict is False
    assert _build_models_parser().parse_args(["validate", "--strict"]).strict is True
