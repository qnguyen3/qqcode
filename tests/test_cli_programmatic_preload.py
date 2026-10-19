from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pytest

from tests.mock.mock_backend_factory import mock_backend_factory
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.entrypoint import _parse_tool_flags, apply_runtime_flags, parse_arguments
from vibe.core import run_programmatic
from vibe.core.config import Backend, SessionLoggingConfig, VibeConfig
from vibe.core.sessions.entries import UsageEntry
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import LLMMessage, OutputFormat, Role


class SpyStreamingFormatter:
    def __init__(self) -> None:
        self.emitted: list[tuple[Role, str | None]] = []

    def on_message_added(self, message: LLMMessage) -> None:
        self.emitted.append((message.role, message.content))

    def on_event(self, _event) -> None:  # No-op for this test
        pass

    def finalize(self) -> str | None:
        return None


def test_run_programmatic_preload_streaming_is_batched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = SpyStreamingFormatter()
    monkeypatch.setattr(
        "vibe.core.programmatic.create_formatter", lambda *_args, **_kwargs: spy
    )

    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([
            mock_llm_chunk(
                content="Decorators are wrappers that modify function behavior.",
                finish_reason="stop",
            )
        ]),
    ):
        cfg = VibeConfig(
            session_logging=SessionLoggingConfig(enabled=False),
            system_prompt_id="tests",
            include_project_context=False,
            include_prompt_detail=False,
            include_model_info=False,
            include_commit_signature=False,
        )

        previous = [
            LLMMessage(
                role=Role.system, content="This system message should be ignored."
            ),
            LLMMessage(
                role=Role.user, content="Previously, you told me about decorators."
            ),
            LLMMessage(
                role=Role.assistant,
                content="Sure, decorators allow you to wrap functions.",
            ),
        ]

        run_programmatic(
            config=cfg,
            prompt="Can you summarize what decorators are?",
            output_format=OutputFormat.STREAMING,
            previous_messages=previous,
            mode="auto-approve",
        )

        roles = [r for r, _ in spy.emitted]
        assert roles == [
            Role.system,
            Role.user,
            Role.assistant,
            Role.user,
            Role.assistant,
        ]
        assert (
            spy.emitted[0][1] == "You are Vibe, a super useful programming assistant."
        )
        assert spy.emitted[1][1] == "Previously, you told me about decorators."
        assert spy.emitted[2][1] == "Sure, decorators allow you to wrap functions."
        assert spy.emitted[3][1] == "Can you summarize what decorators are?"
        assert (
            spy.emitted[4][1]
            == "Decorators are wrappers that modify function behavior."
        )


def test_run_programmatic_ignores_system_messages_in_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = SpyStreamingFormatter()
    monkeypatch.setattr(
        "vibe.core.programmatic.create_formatter", lambda *_args, **_kwargs: spy
    )

    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([mock_llm_chunk(content="Understood.")]),
    ):
        cfg = VibeConfig(
            session_logging=SessionLoggingConfig(enabled=False),
            system_prompt_id="tests",
            include_project_context=False,
            include_prompt_detail=False,
            include_model_info=False,
            include_commit_signature=False,
        )

        run_programmatic(
            config=cfg,
            prompt="Let's move on to practical examples.",
            output_format=OutputFormat.STREAMING,
            previous_messages=[
                LLMMessage(
                    role=Role.system,
                    content="First system message that should be ignored.",
                ),
                LLMMessage(role=Role.user, content="Continue our previous discussion."),
                LLMMessage(
                    role=Role.system,
                    content="Second system message that should be ignored.",
                ),
            ],
            mode="auto-approve",
        )

        roles = [r for r, _ in spy.emitted]
        assert roles == [Role.system, Role.user, Role.user, Role.assistant]
        assert (
            spy.emitted[0][1] == "You are Vibe, a super useful programming assistant."
        )
        assert spy.emitted[1][1] == "Continue our previous discussion."
        assert spy.emitted[2][1] == "Let's move on to practical examples."
        assert spy.emitted[3][1] == "Understood."


def test_run_programmatic_persists_jsonl_session(tmp_path: Path) -> None:
    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([
            mock_llm_chunk(content="persisted answer", finish_reason="stop")
        ]),
    ):
        cfg = VibeConfig(
            session_logging=SessionLoggingConfig(
                enabled=True,
                fsync=False,
                sessions_dir=str(tmp_path),
                save_dir=str(tmp_path / "legacy"),
            ),
            system_prompt_id="tests",
            include_project_context=False,
            include_prompt_detail=False,
            include_model_info=False,
            include_commit_signature=False,
        )

        run_programmatic(
            config=cfg,
            prompt="persist this",
            output_format=OutputFormat.TEXT,
            mode="auto-approve",
        )

    store = JsonlSessionStore(cfg.session_logging)
    summaries = store.list(workdir=None, limit=10)
    assert len(summaries) == 1
    view = store.read(summaries[0].path)
    assert [message.content for message in view.messages()] == [
        "persist this",
        "persisted answer",
    ]
    assert any(isinstance(entry, UsageEntry) for entry in view.entries)


def test_enabled_tools_is_an_alias_of_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qqcode",
            "--enabled-tools",
            "bash",
            "--tools",
            "grep",
            "--tools",
            "read_file",
        ],
    )

    args = parse_arguments()

    assert args.tools == ["bash", "grep", "read_file"]


def test_parse_tool_flags_supports_include_exclude_and_bare_values() -> None:
    enabled, disabled = _parse_tool_flags(
        ["+bash*", "-rm", "read_file"], bare="enabled"
    )
    assert enabled == ["bash*", "read_file"]
    assert disabled == ["rm"]

    excluded_enabled, excluded_disabled = _parse_tool_flags(
        ["+grep", "write_file"], bare="disabled"
    )
    assert excluded_enabled == ["grep"]
    assert excluded_disabled == ["write_file"]


def test_apply_runtime_flags_wires_new_options(tmp_path: Path) -> None:
    prompt_file = tmp_path / "system.md"
    prompt_file.write_text("custom system prompt", encoding="utf-8")
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    session_dir = tmp_path / "sessions"

    config = VibeConfig(
        session_logging=SessionLoggingConfig(
            enabled=True, save_dir=str(tmp_path / "legacy")
        ),
        include_context_files=True,
        enable_update_checks=False,
    )
    args = argparse.Namespace(
        system_prompt=str(prompt_file),
        append_system_prompt=["be terse"],
        no_context_files=True,
        session_dir=str(session_dir),
        no_session=True,
        tools=["+bash", "-edit"],
        exclude_tools=["+grep", "rm"],
        prompt_template=[str(template_dir)],
    )

    dirs = apply_runtime_flags(config, args)

    assert config.system_prompt_override == "custom system prompt"
    assert config.append_system_prompt == ["be terse"]
    assert config.include_context_files is False
    assert config.session_logging.enabled is False
    assert config.session_logging.sessions_dir == str(session_dir)
    assert config.enabled_tools == ["bash", "grep"]
    assert config.disabled_tools == ["edit", "rm"]
    assert dirs == [template_dir]


def test_apply_runtime_flags_reads_literal_prompt_text(tmp_path: Path) -> None:
    config = VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False), enable_update_checks=False
    )
    args = argparse.Namespace(
        system_prompt="literal prompt",
        append_system_prompt=None,
        no_context_files=False,
        session_dir=None,
        no_session=False,
        tools=None,
        exclude_tools=None,
        prompt_template=None,
    )

    apply_runtime_flags(config, args)

    assert config.system_prompt_override == "literal prompt"
    assert config.enabled_tools == []
    assert config.disabled_tools == []


def test_new_flags_do_not_break_programmatic_flow() -> None:
    with mock_backend_factory(
        Backend.MISTRAL,
        lambda provider, **kwargs: FakeBackend([
            mock_llm_chunk(content="still works", finish_reason="stop")
        ]),
    ):
        cfg = VibeConfig(
            session_logging=SessionLoggingConfig(enabled=False),
            system_prompt_id="tests",
            system_prompt_override="You are a test agent.",
            append_system_prompt=["Answer briefly."],
            include_context_files=False,
            include_project_context=False,
            include_prompt_detail=False,
            include_model_info=False,
            include_commit_signature=False,
        )

        result = run_programmatic(
            config=cfg,
            prompt="hello",
            output_format=OutputFormat.TEXT,
            mode="auto-approve",
        )

    assert result == "still works"
