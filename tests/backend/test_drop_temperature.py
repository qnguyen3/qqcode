from __future__ import annotations

import json

from vibe.core.llm.backend.generic import drop_temperature


def test_drop_temperature_removes_only_that_field() -> None:
    body = json.dumps({"model": "m", "temperature": 0.2, "max_tokens": 5}).encode()

    assert json.loads(drop_temperature(body)) == {"model": "m", "max_tokens": 5}


def test_drop_temperature_leaves_a_body_without_it_unchanged() -> None:
    body = json.dumps({"model": "m"}).encode()

    assert drop_temperature(body) == body
