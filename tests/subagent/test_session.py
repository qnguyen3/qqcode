from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.subagent.helpers import make_config, make_task_call
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig
from vibe.core.sessions.entries import CustomEntry
from vibe.core.sessions.store import JSONL_SUFFIX, JsonlSessionStore, SessionView
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import Role


def make_session_config(tmp_path: Path) -> SessionLoggingConfig:
    return SessionLoggingConfig(
        enabled=True,
        fsync=False,
        sessions_dir=str(tmp_path / "sessions"),
        save_dir=str(tmp_path / "legacy"),
    )


def find_child_view(
    store: JsonlSessionStore, parent_session_id: str
) -> tuple[Path, SessionView]:
    for path in store.root.glob(f"*/*{JSONL_SUFFIX}"):
        view = store.read(path)
        if view.header.parent_session_id == parent_session_id:
            return path, view
    raise AssertionError("no child session found")


@pytest.mark.asyncio
async def test_child_session_is_linked_and_records_result(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        session_logging=make_session_config(tmp_path),
        tools={"task": BaseToolConfig(permission=ToolPermission.ALWAYS)},
    )
    backend = FakeBackend([
        mock_llm_chunk(
            content="Delegating.",
            tool_calls=[make_task_call("count files", "count the files", call_id="t1")],
        ),
        mock_llm_chunk(
            content="There are 3 files.",
            prompt_tokens=7,
            completion_tokens=2,
            finish_reason="stop",
        ),
        mock_llm_chunk(content="Reported back.", finish_reason="stop"),
    ])
    parent = Agent(config, auto_approve=True, backend=backend)

    async for _ in parent.act("go"):
        pass

    store = JsonlSessionStore(config.session_logging)
    parent_path = store.find(parent.session_id)
    assert parent_path is not None
    parent_view = store.read(parent_path)
    assert parent_view.header.parent_session_id is None

    child_path, child_view = find_child_view(store, parent.session_id)
    assert child_view.header.cwd == str(tmp_path)

    child_messages = child_view.messages()
    assert [message.role for message in child_messages] == [Role.user, Role.assistant]
    assert child_messages[0].content == "count the files"
    assert child_messages[1].content == "There are 3 files."

    result_entries = [
        entry
        for entry in child_view.entries
        if isinstance(entry, CustomEntry) and entry.key == "subagent_result"
    ]
    assert len(result_entries) == 1
    payload = result_entries[0].value
    assert payload["output"] == "There are 3 files."
    assert payload["turns"] == 1
    assert payload["success"] is True
    assert payload["session_id"] == child_view.header.id

    # The parent session only gains the tool result message (plus a summary
    # custom entry); no child conversation leaks into it.
    parent_messages = parent_view.messages()
    assert [message.role for message in parent_messages] == [
        Role.user,
        Role.assistant,
        Role.tool,
        Role.assistant,
    ]
    assert "There are 3 files." in (parent_messages[2].content or "")

    parent_custom = [
        entry
        for entry in parent_view.entries
        if isinstance(entry, CustomEntry) and entry.key == "subagent_result"
    ]
    assert len(parent_custom) == 1
    assert parent_custom[0].value["child_session_id"] == child_view.header.id


@pytest.mark.asyncio
async def test_child_session_written_in_same_project_slug_dir(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        session_logging=make_session_config(tmp_path),
        tools={"task": BaseToolConfig(permission=ToolPermission.ALWAYS)},
    )
    backend = FakeBackend([
        mock_llm_chunk(content="Delegating.", tool_calls=[make_task_call()]),
        mock_llm_chunk(content="child output", finish_reason="stop"),
        mock_llm_chunk(content="ok", finish_reason="stop"),
    ])
    parent = Agent(config, auto_approve=True, backend=backend)

    async for _ in parent.act("go"):
        pass

    store = JsonlSessionStore(config.session_logging)
    parent_path = store.find(parent.session_id)
    child_path, child_view = find_child_view(store, parent.session_id)

    assert parent_path is not None
    assert child_view.header.cwd == str(tmp_path)
    assert parent_path.parent == child_path.parent
