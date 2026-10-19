from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import ModelConfig, ModelPricing, SessionLoggingConfig, VibeConfig
from vibe.sdk import AgentOptions, MessageDeltaEvent, create_agent


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
async def test_parallel_sessions_do_not_mix_state(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    backend_a = FakeBackend([
        mock_llm_chunk(
            content="response from A",
            finish_reason="stop",
            prompt_tokens=11,
            completion_tokens=4,
        )
    ])
    backend_b = FakeBackend([
        mock_llm_chunk(
            content="response from B",
            finish_reason="stop",
            prompt_tokens=23,
            completion_tokens=9,
        )
    ])

    async def _run_session(prompt_text: str, backend: FakeBackend):
        options = AgentOptions(config=cfg, auto_approve=True, working_dir=tmp_path)
        deltas: list[str] = []
        async with create_agent(options, backend=backend) as session:
            async for ev in session.prompt(prompt_text):
                if isinstance(ev, MessageDeltaEvent):
                    deltas.append(ev.delta)
            return (
                session.session_id,
                "".join(deltas),
                [m.content for m in session.messages()[1:]],
                session.stats().session_prompt_tokens,
            )

    (
        (id_a, text_a, msgs_a, tokens_a),
        (id_b, text_b, msgs_b, tokens_b),
    ) = await asyncio.gather(
        _run_session("hello A", backend_a), _run_session("hello B", backend_b)
    )

    assert id_a != id_b
    assert text_a == "response from A"
    assert text_b == "response from B"
    assert msgs_a == ["hello A", "response from A"]
    assert msgs_b == ["hello B", "response from B"]
    assert tokens_a == 11
    assert tokens_b == 23
