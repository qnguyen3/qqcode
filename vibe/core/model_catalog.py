from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_PATH = DATA_DIR / "models.generated.json"
CURATED_PATH = DATA_DIR / "models.curated.toml"
SNAPSHOT_MAX_AGE = timedelta(days=30)


def models_digest(models: list[dict[str, Any]]) -> str:
    """Return a sha256 of the model list that ignores key order."""
    canonical = json.dumps(
        models, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_model_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    """Read the generated model snapshot as a JSON object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def dump_model_snapshot(payload: dict[str, Any]) -> str:
    """Serialize a snapshot with stable, reviewable formatting."""
    return f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"


def snapshot_problems(
    payload: dict[str, Any], *, now: datetime
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for a snapshot read from disk.

    Errors mean the snapshot cannot be trusted: the schema is unknown, the
    model list was edited by hand, or the timestamp is unusable. Warnings only
    flag a snapshot older than ``SNAPSHOT_MAX_AGE``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        errors.append(
            f"schema_version is {payload.get('schema_version')!r}, "
            f"expected {SNAPSHOT_SCHEMA_VERSION}"
        )
    models = payload.get("models")
    if not isinstance(models, list):
        errors.append("models must be a list")
        return errors, warnings
    if payload.get("models_sha256") != models_digest(models):
        errors.append(
            "models_sha256 does not match models; regenerate the snapshot with "
            "scripts/generate_models.py instead of editing it"
        )

    try:
        built_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except ValueError:
        errors.append("generated_at is not an ISO 8601 timestamp")
        return errors, warnings
    if built_at.tzinfo is None:
        errors.append("generated_at must include a timezone offset")
        return errors, warnings
    if now - built_at > SNAPSHOT_MAX_AGE:
        warnings.append(
            f"snapshot was generated on {built_at:%Y-%m-%d}; run "
            "scripts/generate_models.py to refresh it"
        )
    return errors, warnings
