from __future__ import annotations

import base64
from pathlib import Path

from tests.backend.test_image_encoding import PNG_BYTES, write_png
from vibe.core.config import SessionLoggingConfig
from vibe.core.images import describe_image, load_image_part
from vibe.core.sessions.entries import (
    MessageEntry,
    SessionHeader,
    new_entry_id,
    now_iso,
)
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, Role, message_for_storage


def make_store(tmp_path: Path) -> JsonlSessionStore:
    config = SessionLoggingConfig(
        enabled=True, sessions_dir=str(tmp_path), save_dir=str(tmp_path / "legacy")
    )
    return JsonlSessionStore(config)


def test_session_log_stores_image_references_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session_id = new_entry_id()
    path = store.session_path(workdir, session_id)
    store.write_entries(
        path, SessionHeader(id=session_id, timestamp=now_iso(), cwd=str(workdir)), []
    )

    image_path = write_png(workdir / "pixel.png")
    message = LLMMessage(
        role=Role.user,
        content="look",
        images=[load_image_part(describe_image(image_path))],
    )
    store.append(
        MessageEntry(
            id=new_entry_id(),
            parent_id=session_id,
            timestamp=now_iso(),
            message=message_for_storage(message),
        ),
        path,
    )

    raw = path.read_text(encoding="utf-8")
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    assert encoded not in raw
    assert "pixel.png" in raw

    view = store.read(path)
    stored = view.messages()[0]
    assert stored.images[0].data == ""
    assert stored.images[0].source_path == str(image_path)

    reloaded = load_image_part(stored.images[0])
    assert base64.b64decode(reloaded.data) == PNG_BYTES
