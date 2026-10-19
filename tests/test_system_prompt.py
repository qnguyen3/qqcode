from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

from vibe.core.config import VibeConfig
from vibe.core.prompts.sections import SECTION_ORDER
from vibe.core.system_prompt import build_prompt_sections, get_universal_system_prompt
from vibe.core.tools.manager import ToolManager


def _make_config(**overrides: Any) -> VibeConfig:
    defaults: dict[str, Any] = {
        "system_prompt_id": "tests",
        "include_project_context": False,
        "include_prompt_detail": True,
        "include_model_info": True,
        "include_commit_signature": True,
    }
    return VibeConfig(**{**defaults, **overrides})


def test_get_universal_system_prompt_includes_windows_prompt_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")

    config = VibeConfig(
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=True,
        include_model_info=False,
        include_commit_signature=False,
    )
    tool_manager = ToolManager(config)

    prompt = get_universal_system_prompt(tool_manager, config)

    assert "You are Vibe, a super useful programming assistant." in prompt
    assert (
        "The operating system is Windows with shell `C:\\Windows\\System32\\cmd.exe`"
        in prompt
    )
    assert "DO NOT use Unix commands like `ls`, `grep`, `cat`" in prompt
    assert "Use: `dir` (Windows) for directory listings" in prompt
    assert "Use: backslashes (\\\\) for paths" in prompt
    assert "Check command availability with: `where command` (Windows)" in prompt
    assert "Script shebang: Not applicable on Windows" in prompt


def test_interactive_prompt_keeps_legacy_content_blocks() -> None:
    config = _make_config()
    prompt = get_universal_system_prompt(ToolManager(config), config)

    assert prompt.startswith(config.system_prompt)
    assert "When you want to commit changes" in prompt
    assert "Your model name is: `devstral-2`" in prompt
    assert "The operating system is Linux with shell `sh`" in prompt
    assert "PLAN MODE" not in prompt


def test_prompt_sections_follow_canonical_order() -> None:
    config = _make_config()
    sections = build_prompt_sections(ToolManager(config), config, mode="plan")
    names = sections.names()

    assert names == [name for name in SECTION_ORDER if name in names]
    assert "mode" in names
    assert names.index("mode") < names.index("os_env")


def test_context_files_are_wrapped_as_project_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    config = _make_config(
        workdir=tmp_path, include_project_context=True, include_prompt_detail=False
    )

    prompt = get_universal_system_prompt(ToolManager(config), config)

    assert '<project_instructions path="' in prompt
    assert "project rules" in prompt


def test_legacy_project_doc_used_when_context_files_disabled(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    config = _make_config(
        workdir=tmp_path,
        include_project_context=True,
        include_prompt_detail=False,
        include_context_files=False,
    )

    prompt = get_universal_system_prompt(ToolManager(config), config)

    assert "<project_instructions" not in prompt
    assert "project rules" in prompt
