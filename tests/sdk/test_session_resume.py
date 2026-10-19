from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import ModelConfig, ModelPricing, SessionLoggingConfig, VibeConfig
from vibe.core.sessions.entries import MessageEntry
from vibe.core.sessions.store import JsonlSessionStore
from vibe.sdk import AgentOptions, AgentSession, SessionRef, create_agent


def _make_config(tmp_path: Path) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=True,
            fsync=False,
            sessions_dir=str(tmp_path),
            save_dir=str(tmp_path / "legacy"),
        ),
        system_prompt_id="tests",
        active_model="devstral-latest",
        models=[
            ModelConfig(
                name="mistral-vibe-cli-latest",
                provider="mistral",
                alias="devstral-latest",
                pricing=ModelPricing(input_per_million=0.4, output_per_million=2.0),
            )
        ],
        include_project_context=False,
        include_prompt_detail=False,
        include_model_info=False,
        include_commit_signature=False,
    )


@pytest.mark.asyncio
async def test_session_resume_and_fork_preserve_history_and_stats(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    first_backend = FakeBackend([
        mock_llm_chunk(
            content="first answer",
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=10,
        ),
        mock_llm_chunk(
            content="second answer",
            finish_reason="stop",
            prompt_tokens=30,
            completion_tokens=15,
        ),
    ])

    async with create_agent(
        AgentOptions(config=cfg, auto_approve=True, working_dir=tmp_path),
        backend=first_backend,
    ) as initial:
        async for _ in initial.prompt("question 1"):
            pass
        async for _ in initial.prompt("question 2"):
            pass
        original_id = initial.session_id

    summaries = AgentSession.list_sessions(workdir=tmp_path, sessions_dir=tmp_path)
    assert any(s.session_id == original_id for s in summaries)

    # Resume existing session by ID
    resume_backend = FakeBackend([
        mock_llm_chunk(
            content="third answer",
            finish_reason="stop",
            prompt_tokens=40,
            completion_tokens=20,
        )
    ])
    async with create_agent(
        AgentOptions(
            config=cfg,
            auto_approve=True,
            working_dir=tmp_path,
            session=SessionRef(session_id=original_id),
        ),
        backend=resume_backend,
    ) as resumed:
        assert resumed.session_id == original_id
        assert resumed.stats().session_prompt_tokens == 50
        assert resumed.stats().session_completion_tokens == 25
        assert [m.content for m in resumed.messages()[1:]] == [
            "question 1",
            "first answer",
            "question 2",
            "second answer",
        ]
        async for _ in resumed.prompt("question 3"):
            pass
        assert resumed.stats().session_prompt_tokens == 90

    # Fork at the end of turn 1 (second MessageEntry)
    store = JsonlSessionStore(cfg.session_logging)
    source_path = store.find(original_id)
    assert source_path is not None
    view = store.read(source_path)
    msg_entries = [e for e in view.entries if isinstance(e, MessageEntry)]
    first_turn_end_id = msg_entries[1].id

    fork_backend = FakeBackend([
        mock_llm_chunk(
            content="forked answer",
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=6,
        )
    ])
    async with create_agent(
        AgentOptions(
            config=cfg,
            auto_approve=True,
            working_dir=tmp_path,
            session=SessionRef(
                session_id=original_id, fork_at_entry_id=first_turn_end_id
            ),
        ),
        backend=fork_backend,
    ) as forked:
        assert forked.session_id != original_id
        assert [m.content for m in forked.messages()[1:]] == [
            "question 1",
            "first answer",
        ]
        async for _ in forked.prompt("forked question"):
            pass
        assert [m.content for m in forked.messages()[1:]] == [
            "question 1",
            "first answer",
            "forked question",
            "forked answer",
        ]
