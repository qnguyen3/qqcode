from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from textual.widgets import Markdown

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.textual_ui.app import VibeApp
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, ThinkingLevel, VibeConfig
from vibe.core.sessions.entries import CustomEntry, ModelChangeEntry
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.types import BaseEvent, LLMMessage, Role


async def _wait_for(
    pilot, condition: Callable[[], object | None], timeout: float = 3.0
) -> object | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        await pilot.pause(0.05)
    return None


class CapturingAgent(Agent):
    def __init__(self) -> None:
        self._stub_messages: list = []
        self.stats = SimpleNamespace(context_tokens=0)
        self.approval_callback = None
        self.prompts: list[str] = []

    @property
    def messages(self) -> list:
        return self._stub_messages

    async def act(self, msg: str) -> AsyncGenerator[BaseEvent]:
        self.prompts.append(msg)
        if False:
            yield


def _patch_agent_init(monkeypatch: pytest.MonkeyPatch, agent: Agent) -> None:
    async def _fake_initialize(self: VibeApp) -> None:
        if self.agent or self._agent_initializing:
            return

        self._agent_initializing = True
        try:
            self.agent = agent
        finally:
            self._agent_initializing = False
            self._agent_init_task = None

    monkeypatch.setattr(VibeApp, "_initialize_agent", _fake_initialize, raising=True)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / ".qqcode-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def templates_dir(workdir: Path) -> Path:
    path = workdir / "templates"
    path.mkdir()
    (path / "pr.md").write_text(
        """---
description: Create a pull request
argument-hint: <branch>
---

Open a pull request for $1 into ${2:-main}.
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def vibe_config(workdir: Path) -> VibeConfig:
    return VibeConfig(
        workdir=workdir,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
    )


@pytest.mark.asyncio
async def test_prompt_template_runs_end_to_end(
    templates_dir: Path, vibe_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=vibe_config, prompt_template_dirs=[templates_dir])
    agent = CapturingAgent()
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await pilot.press(*'/pr "feature branch"')
        await pilot.press("enter")

        await _wait_for(pilot, lambda: agent.prompts)

    assert agent.prompts == ["Open a pull request for feature branch into main."]


def test_prompt_template_registered_in_help(
    templates_dir: Path, vibe_config: VibeConfig
) -> None:
    app = VibeApp(config=vibe_config, prompt_template_dirs=[templates_dir])

    command = app.commands.find_command("/pr")

    assert command is not None
    assert command.template == "pr"
    assert "`/pr`" in app.commands.get_help_text()


def test_template_name_colliding_with_builtin_loses(
    workdir: Path, vibe_config: VibeConfig
) -> None:
    collision_dir = workdir / "collision"
    collision_dir.mkdir()
    (collision_dir / "help.md").write_text("custom help", encoding="utf-8")

    app = VibeApp(config=vibe_config, prompt_template_dirs=[collision_dir])

    command = app.commands.find_command("/help")
    assert command is not None
    assert command.handler == "_show_help"
    assert "template:help" not in app.commands.commands


def test_new_commands_are_registered_in_help(vibe_config: VibeConfig) -> None:
    app = VibeApp(config=vibe_config)
    help_text = app.commands.get_help_text()

    for alias in (
        "/model",
        "/scoped-models",
        "/thinking",
        "/new",
        "/resume",
        "/name",
        "/session",
        "/export",
        "/import",
        "/fork",
        "/copy",
    ):
        assert alias in help_text
    assert "Ctrl+P" in help_text
    assert "Ctrl+Y" in help_text


@pytest.fixture
def session_config(tmp_path: Path) -> SessionLoggingConfig:
    return SessionLoggingConfig(
        enabled=True,
        fsync=False,
        sessions_dir=str(tmp_path / "sessions"),
        save_dir=str(tmp_path / "legacy"),
    )


@pytest.fixture
def session_app_config(
    workdir: Path, session_config: SessionLoggingConfig
) -> VibeConfig:
    return VibeConfig(
        workdir=workdir,
        session_logging=session_config,
        enable_update_checks=False,
        include_project_context=False,
    )


def make_real_agent(config: VibeConfig) -> Agent:
    return Agent(
        config,
        mode=None,
        backend=FakeBackend([mock_llm_chunk(content="ack", finish_reason="stop")]),
    )


def read_session_entries(config: VibeConfig) -> list:
    store = JsonlSessionStore(config.session_logging)
    summaries = store.list(workdir=None, limit=10)
    assert summaries
    return store.read(summaries[0].path).entries


async def submit(pilot, command: str) -> None:
    await pilot.press(*command)
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_model_alias_switches_and_records_entries(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_alias = next(
        model.alias
        for model in session_app_config.models
        if model.alias != session_app_config.active_model
    )
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, f"/model {target_alias}")
        await _wait_for(pilot, lambda: session_app_config.active_model == target_alias)
        await pilot.pause(0.1)

    entries = read_session_entries(session_app_config)
    model_changes = [entry for entry in entries if isinstance(entry, ModelChangeEntry)]
    assert model_changes[-1].model_alias == target_alias
    custom = {
        entry.key: entry.value for entry in entries if isinstance(entry, CustomEntry)
    }
    assert custom["model"]["alias"] == target_alias


@pytest.mark.asyncio
async def test_thinking_command_sets_level_and_records_entry(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/thinking high")
        await _wait_for(pilot, lambda: agent.thinking_level is ThinkingLevel.HIGH)
        await pilot.pause(0.1)

    assert agent.thinking_level is ThinkingLevel.HIGH
    entries = read_session_entries(session_app_config)
    custom = {
        entry.key: entry.value for entry in entries if isinstance(entry, CustomEntry)
    }
    assert custom["thinking_level"] == {"level": "high"}


@pytest.mark.asyncio
async def test_name_command_persists_custom_entry(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/name My Session")
        await _wait_for(pilot, lambda: app._session_name == "My Session")
        await pilot.pause(0.1)

    entries = read_session_entries(session_app_config)
    custom = {
        entry.key: entry.value for entry in entries if isinstance(entry, CustomEntry)
    }
    assert custom["name"] == {"name": "My Session"}


@pytest.mark.asyncio
async def test_session_command_shows_info(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="hello"))
    agent.add_message(LLMMessage(role=Role.assistant, content="hi there"))
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/session")
        await _wait_for(
            pilot,
            lambda: next(
                (
                    widget
                    for widget in app.query(".user-command-message")
                    if "Session Info" in str(widget.query_one(Markdown).source)
                ),
                None,
            ),
        )
        source = " ".join(
            str(widget.query_one(Markdown).source)
            for widget in app.query(".user-command-message")
        )

    assert "Session Info" in source
    assert session_app_config.active_model in source


@pytest.mark.asyncio
async def test_new_command_resets_messages_and_session_id(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="before reset"))
    old_session_id = agent.session_id
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/new")
        await _wait_for(pilot, lambda: agent.session_id != old_session_id)
        await pilot.pause(0.1)

    assert len(agent.messages) == 1
    assert agent.session_id != old_session_id
    assert session_app_config.session_logging.enabled


@pytest.mark.asyncio
async def test_fork_command_switches_to_forked_session(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="first user message"))
    agent.add_message(LLMMessage(role=Role.assistant, content="first answer"))
    original_path = agent.interaction_logger.filepath
    assert original_path is not None
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/fork")
        await _wait_for(pilot, lambda: app._current_bottom_app.name == "Fork")
        await pilot.press("enter")
        await _wait_for(
            pilot, lambda: agent.interaction_logger.filepath != original_path
        )
        await pilot.pause(0.1)

    forked_path = agent.interaction_logger.filepath
    assert forked_path is not None
    store = JsonlSessionStore(session_app_config.session_logging)
    forked_view = store.read(forked_path)
    original_view = store.read(original_path)
    assert forked_view.header.parent_session_id == original_view.header.id
    assert [message.content for message in forked_view.messages()] == [
        "first user message"
    ]


@pytest.mark.asyncio
async def test_export_command_writes_markdown(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="export me"))
    _patch_agent_init(monkeypatch, agent)
    target = tmp_path / "exported.md"

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await app._export_session(f"/export {target}")

    assert "export me" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_export_and_import_roundtrip(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="roundtrip message"))
    agent.add_message(LLMMessage(role=Role.assistant, content="roundtrip answer"))
    _patch_agent_init(monkeypatch, agent)
    exported = tmp_path / "roundtrip.jsonl"

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await app._export_session(f"/export {exported}")
        await app._new_session()
        assert len(agent.messages) == 1
        await app._import_session(f"/import {exported}")

    assert [message.content for message in agent.messages[1:]] == [
        "roundtrip message",
        "roundtrip answer",
    ]


@pytest.mark.asyncio
async def test_resume_command_by_id(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="keep me"))
    original_id = agent.session_id
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/new")
        await _wait_for(pilot, lambda: agent.session_id != original_id)

        await submit(pilot, f"/resume {original_id[:8]}")
        await _wait_for(pilot, lambda: agent.session_id == original_id)

    assert any(message.content == "keep me" for message in agent.messages)


@pytest.mark.asyncio
async def test_resume_restores_model_and_thinking_preferences(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = session_app_config.models
    first_alias = models[0].alias
    second_alias = models[1].alias
    session_app_config.active_model = first_alias
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="remember me"))
    agent.set_model(second_alias)
    agent.set_thinking_level(ThinkingLevel.HIGH, record=True)
    original_id = agent.session_id
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/new")
        await _wait_for(pilot, lambda: agent.session_id != original_id)

        agent.set_model(first_alias)
        agent.set_thinking_level(ThinkingLevel.LOW, record=True)

        await submit(pilot, f"/resume {original_id[:8]}")
        await _wait_for(pilot, lambda: agent.session_id == original_id)

    assert session_app_config.active_model == second_alias
    assert agent.thinking_level is ThinkingLevel.HIGH


@pytest.mark.asyncio
async def test_scoped_models_screen_saves_scope(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/scoped-models")
        await _wait_for(pilot, lambda: app._current_bottom_app.name == "ScopedModels")

        first_alias = session_app_config.models[0].alias
        await pilot.press("space")
        await pilot.press("escape")
        await _wait_for(
            pilot,
            lambda: session_app_config.enabled_models
            and first_alias not in session_app_config.enabled_models,
        )

    assert first_alias not in session_app_config.enabled_models
    assert len(session_app_config.enabled_models) == len(session_app_config.models) - 1


@pytest.mark.asyncio
async def test_ctrl_p_cycles_models_in_scope(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = session_app_config.models[0].alias
    second = session_app_config.models[1].alias
    session_app_config.enabled_models = [first, second]
    session_app_config.active_model = first
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await pilot.press("ctrl+p")
        await _wait_for(pilot, lambda: session_app_config.active_model == second)


@pytest.mark.asyncio
async def test_copy_command_uses_clipboard(
    session_app_config: VibeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []
    monkeypatch.setattr("vibe.cli.clipboard.pyperclip.copy", copied.append)
    app = VibeApp(config=session_app_config)
    agent = make_real_agent(session_app_config)
    agent.add_message(LLMMessage(role=Role.user, content="question"))
    agent.add_message(LLMMessage(role=Role.assistant, content="copy this answer"))
    _patch_agent_init(monkeypatch, agent)

    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: app.agent is not None)
        await submit(pilot, "/copy")
        await _wait_for(pilot, lambda: copied)

    assert copied == ["copy this answer"]
