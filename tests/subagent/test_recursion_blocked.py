from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from tests.subagent.helpers import make_config
from vibe.core.agent import Agent
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.llm.format import get_active_tool_classes
from vibe.core.subagent import SubAgentRunner
from vibe.core.tools.builtins.task import TaskArgs

READ_ONLY = {"read_file", "grep", "find", "ls"}


def make_parent(tmp_path: Path, **overrides: Any) -> Agent:
    backend = FakeBackend([mock_llm_chunk(content="child done", finish_reason="stop")])
    return Agent(make_config(tmp_path, **overrides), auto_approve=True, backend=backend)


@pytest.mark.asyncio
async def test_child_toolset_blocks_task_and_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vibe.core.subagent as subagent_module

    created: list[Agent] = []
    real_agent = subagent_module.Agent

    def spy(config: Any, **kwargs: Any) -> Agent:
        agent = real_agent(config, **kwargs)
        created.append(agent)
        return agent

    monkeypatch.setattr(subagent_module, "Agent", spy)

    parent = make_parent(tmp_path)
    result = await SubAgentRunner(parent).run(
        TaskArgs(description="probe tools", prompt="report your tools")
    )

    assert result.success is True
    assert len(created) == 1

    child = created[0]
    names = {
        tool_class.get_name()
        for tool_class in get_active_tool_classes(child.tool_manager, child.config)
    }
    assert "task" not in names
    assert "skill" not in names
    assert names == READ_ONLY


def test_child_config_filters_forbidden_and_sandboxes_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_config = VibeConfig(
        enabled_tools=["task", "bash", "skill"],
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        workdir=tmp_path,
    )

    def fake_load(
        self: SubAgentRunner, name: str, base: VibeConfig, *, allow_fallback: bool
    ) -> VibeConfig:
        return agent_config.model_copy(deep=True)

    monkeypatch.setattr(SubAgentRunner, "_load_agent_config", fake_load)

    parent = make_parent(tmp_path)
    runner = SubAgentRunner(parent)

    config = runner._build_child_config(
        TaskArgs(description="d", prompt="p"), parent.config.subagent
    )

    assert "task" not in config.enabled_tools
    assert "skill" not in config.enabled_tools
    assert "bash" in config.enabled_tools
    assert "task" in config.disabled_tools
    assert "skill" in config.disabled_tools

    for name in ("read_file", "write_file", "edit", "grep", "find", "ls"):
        assert str(tmp_path) in config.tools[name].path_allowlist


def test_child_config_defaults_to_read_only_when_parent_has_no_tool_filter(
    tmp_path: Path,
) -> None:
    parent = make_parent(tmp_path, enabled_tools=[])
    runner = SubAgentRunner(parent)

    config = runner._build_child_config(
        TaskArgs(description="d", prompt="p"), parent.config.subagent
    )

    assert set(config.enabled_tools) == READ_ONLY
