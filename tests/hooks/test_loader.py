from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from vibe.core.config import HooksConfig, SessionLoggingConfig, VibeConfig
from vibe.core.hooks.events import HookDecision, ToolCallEventHook
from vibe.core.hooks.loader import load_hooks
from vibe.core.hooks.registry import HookRegistry


def make_config(
    tmp_path: Path, *, hooks: HooksConfig | None = None, workdir: Path | None = None
) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        workdir=workdir or tmp_path,
        hooks=hooks or HooksConfig(),
    )


def make_event(tool_name: str = "bash") -> ToolCallEventHook:
    return ToolCallEventHook(
        tool_name=tool_name, tool_call_id="call_1", args={"command": "ls"}
    )


def write_hook(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_hook_module_from_configured_path(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(
        hooks_dir,
        "guard.py",
        """
from vibe.core.hooks.api import deny

def register(registry):
    registry.register("tool_call", lambda event: deny("from file"))
""",
    )
    config = make_config(tmp_path, hooks=HooksConfig(paths=[str(hooks_dir)]))

    registry = load_hooks(config)

    decision: HookDecision = await_dispatch(registry, make_event())
    assert decision.action == "deny"
    assert decision.reason == "from file"


def test_import_error_is_skipped_and_other_modules_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(hooks_dir, "broken.py", "raise RuntimeError('broken import')\n")
    write_hook(
        hooks_dir,
        "good.py",
        """
from vibe.core.hooks.api import deny

def register(registry):
    registry.register("tool_call", lambda event: deny("good"))
""",
    )
    config = make_config(tmp_path, hooks=HooksConfig(paths=[str(hooks_dir)]))

    with caplog.at_level(logging.WARNING, logger="vibe"):
        registry = load_hooks(config)

    assert registry.has_handlers("tool_call")
    assert any("broken import" in record.message for record in caplog.records)


def test_disabled_hooks_are_not_loaded(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(
        hooks_dir,
        "guard.py",
        """
from vibe.core.hooks.api import deny

def register(registry):
    registry.register("tool_call", lambda event: deny("should not load"))
""",
    )
    config = make_config(
        tmp_path, hooks=HooksConfig(enabled=False, paths=[str(hooks_dir)])
    )

    registry = load_hooks(config)

    assert registry.registered_types() == []


def test_project_hooks_are_loaded_from_workdir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_hook(
        project / ".qqcode" / "hooks",
        "project_hook.py",
        """
from vibe.core.hooks.api import deny

def register(registry):
    registry.register("tool_call", lambda event: deny("project"))
""",
    )
    config = make_config(tmp_path, workdir=project)

    registry = load_hooks(config)

    assert registry.has_handlers("tool_call")


def test_decorated_bindings_are_auto_registered(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(
        hooks_dir,
        "decorated.py",
        """
from vibe.core.hooks.api import deny, on_tool_call

@on_tool_call("bash")
def guard(event):
    return deny("bash blocked")
""",
    )
    config = make_config(tmp_path, hooks=HooksConfig(paths=[str(hooks_dir)]))

    registry = load_hooks(config)

    assert await_dispatch(registry, make_event("bash")).action == "deny"
    assert await_dispatch(registry, make_event("todo")).action == "allow"


def test_register_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(
        hooks_dir,
        "bad_register.py",
        "def register(registry):\n    raise RuntimeError('register boom')\n",
    )
    config = make_config(tmp_path, hooks=HooksConfig(paths=[str(hooks_dir)]))

    with caplog.at_level(logging.WARNING, logger="vibe"):
        registry = load_hooks(config)

    assert registry.registered_types() == []
    assert any("register boom" in record.message for record in caplog.records)


def test_registry_uses_configured_timeout(tmp_path: Path) -> None:
    config = make_config(tmp_path, hooks=HooksConfig(timeout=2.5))

    registry = load_hooks(config)

    assert registry.timeout == 2.5


def test_module_without_register_or_bindings_is_tolerated(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    write_hook(hooks_dir, "empty.py", "VALUE = 1\n")
    config = make_config(tmp_path, hooks=HooksConfig(paths=[str(hooks_dir)]))

    registry = load_hooks(config)

    assert isinstance(registry, HookRegistry)
    assert registry.registered_types() == []


def await_dispatch(registry: HookRegistry, event: ToolCallEventHook) -> HookDecision:
    return asyncio.run(registry.dispatch(event))
