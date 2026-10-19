from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.messages import ErrorMessage
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.types import BaseEvent


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
def skill_file(workdir: Path) -> Path:
    skill_path = workdir / ".qqcode" / "skills" / "test-skill"
    skill_path.mkdir(parents=True)
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text(
        """---
name: test-skill
description: A test skill.
---

# Test Skill

Follow these steps.
""",
        encoding="utf-8",
    )
    return skill_file


@pytest.fixture
def vibe_config(workdir: Path) -> VibeConfig:
    return VibeConfig(
        workdir=workdir,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
    )


@pytest.fixture
def hidden_skill_file(workdir: Path) -> Path:
    hidden_path = workdir / ".qqcode" / "skills" / "quiet-skill"
    hidden_path.mkdir(parents=True)
    skill_file = hidden_path / "SKILL.md"
    skill_file.write_text(
        """---
name: quiet-skill
description: Hidden from the model.
disable-model-invocation: true
---

# Quiet Skill

Secret instructions.
""",
        encoding="utf-8",
    )
    return skill_file


@pytest.fixture
def vibe_app(vibe_config: VibeConfig) -> VibeApp:
    return VibeApp(config=vibe_config)


@pytest.mark.asyncio
async def test_hidden_skill_is_callable_explicitly(
    hidden_skill_file: Path, vibe_app: VibeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CapturingAgent()
    _patch_agent_init(monkeypatch, agent)

    async with vibe_app.run_test() as pilot:
        await pilot.press(*"/skill:quiet-skill")
        await pilot.press("enter")

        await _wait_for(pilot, lambda: agent.prompts)

    assert len(agent.prompts) == 1
    assert "# Quiet Skill" in agent.prompts[0]
    assert "Secret instructions." in agent.prompts[0]


@pytest.mark.asyncio
async def test_skill_command_expands_and_appends_args(
    skill_file: Path, vibe_app: VibeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CapturingAgent()
    _patch_agent_init(monkeypatch, agent)

    async with vibe_app.run_test() as pilot:
        await pilot.press(*"/skill:test-skill extract pages 2-3")
        await pilot.press("enter")

        await _wait_for(pilot, lambda: agent.prompts)

    assert len(agent.prompts) == 1
    message = agent.prompts[0]
    assert message.startswith(
        f'<skill name="test-skill" location="{skill_file.resolve()}">'
    )
    assert "# Test Skill" in message
    assert "Follow these steps." in message
    assert "description: A test skill." not in message
    assert "extract pages 2-3" in message
    assert message.endswith("</skill>")


@pytest.mark.asyncio
async def test_unknown_skill_command_shows_error(
    skill_file: Path, vibe_app: VibeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CapturingAgent()
    _patch_agent_init(monkeypatch, agent)

    async with vibe_app.run_test() as pilot:
        await pilot.press(*"/skill:missing")
        await pilot.press("enter")
        await pilot.pause(0.2)

        errors = list(vibe_app.query(ErrorMessage))

    assert agent.prompts == []
    assert errors
    assert "missing" in errors[0]._error


@pytest.mark.asyncio
async def test_skill_command_without_name_shows_error(
    skill_file: Path, vibe_app: VibeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = CapturingAgent()
    _patch_agent_init(monkeypatch, agent)

    async with vibe_app.run_test() as pilot:
        await pilot.press(*"/skill: ")
        await pilot.press("enter")
        await pilot.pause(0.2)

        errors = list(vibe_app.query(ErrorMessage))

    assert agent.prompts == []
    assert errors
    assert "No skill name" in errors[0]._error
