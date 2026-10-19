from __future__ import annotations

import time

from pydantic import ValidationError
import pytest

from vibe.core.sessions.entries import (
    CompactionEntry,
    ContextEditEntry,
    CustomEntry,
    HeadMarkerEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionHeader,
    UsageEntry,
    new_entry_id,
    parse_entry,
)
from vibe.core.types import LLMMessage, Role


def _all_entries() -> list[
    SessionHeader
    | MessageEntry
    | CompactionEntry
    | ModelChangeEntry
    | UsageEntry
    | CustomEntry
    | HeadMarkerEntry
    | ContextEditEntry
]:
    return [
        SessionHeader(id="s1", timestamp="2024-01-01T00:00:00Z", cwd="/tmp"),
        MessageEntry(
            id="m1",
            timestamp="2024-01-01T00:00:01Z",
            message=LLMMessage(role=Role.user, content="hello"),
        ),
        CompactionEntry(
            id="c1",
            timestamp="2024-01-01T00:00:02Z",
            first_kept_id="m1",
            summary="summary",
            tokens_before=100,
            tokens_after=10,
        ),
        ModelChangeEntry(
            id="mc1",
            timestamp="2024-01-01T00:00:03Z",
            model_alias="devstral",
            provider="mistral",
        ),
        UsageEntry(
            id="u1",
            timestamp="2024-01-01T00:00:04Z",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
        ),
        CustomEntry(
            id="x1", timestamp="2024-01-01T00:00:05Z", key="tag", value={"a": 1}
        ),
        HeadMarkerEntry(
            id="h1", timestamp="2024-01-01T00:00:06Z", keep_from_id=None, reason="clear"
        ),
        ContextEditEntry(
            id="e1",
            timestamp="2024-01-01T00:00:07Z",
            target_id="m1",
            action="replace",
            replacement="rewritten",
        ),
    ]


@pytest.mark.parametrize("entry", _all_entries())
def test_entry_roundtrip(entry) -> None:
    payload = entry.model_dump_json()
    parsed = parse_entry(payload)
    assert parsed == entry
    assert type(parsed) is type(entry)
    assert parsed.type == entry.type


def test_discriminated_union_from_python_dict() -> None:
    entry = parse_entry({
        "type": "usage",
        "id": "u1",
        "timestamp": "2024-01-01T00:00:00Z",
        "prompt_tokens": 3,
        "completion_tokens": 4,
    })
    assert isinstance(entry, UsageEntry)
    assert entry.cache_read_tokens == 0
    assert entry.cache_write_tokens == 0


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_entry({"type": "nope", "id": "x", "timestamp": "t"})


def test_header_defaults() -> None:
    header = SessionHeader(id="s1", timestamp="t", cwd="/tmp")
    assert header.version == 1
    assert header.parent_session_id is None
    assert header.git_branch is None


def test_new_entry_ids_are_unique_and_time_sorted() -> None:
    ids = [new_entry_id() for _ in range(1000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_new_entry_ids_respect_explicit_timestamps() -> None:
    now_ms = time.time_ns() // 1_000_000
    early = new_entry_id(timestamp_ms=now_ms + 1)
    later = new_entry_id(timestamp_ms=now_ms + 2)
    assert early < later
