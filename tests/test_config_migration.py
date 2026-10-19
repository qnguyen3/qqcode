from __future__ import annotations

from vibe.core.config import ScriptToolConfig, VibeConfig
from vibe.core.tools.manager import ToolManager


def test_tools_script_missing_defaults_to_disabled_and_unregistered() -> None:
    loaded = VibeConfig()

    assert loaded.tool_runtime.script == ScriptToolConfig()
    assert loaded.tool_runtime.script.enabled is False
    assert "run_tools_script" not in ToolManager(loaded).available_tools()


def test_tools_script_enabled_registers_run_tools_script() -> None:
    loaded = VibeConfig.model_validate({
        "tools": {
            "script": {
                "enabled": True,
                "timeout": 12.0,
                "max_calls": 5,
                "max_output_bytes": 2048,
                "memory_mb": 128,
            }
        }
    })

    assert loaded.tool_runtime.script.enabled is True
    assert loaded.tool_runtime.script.timeout == 12.0
    assert loaded.tool_runtime.script.max_calls == 5
    assert loaded.tool_runtime.script.max_output_bytes == 2048
    assert loaded.tool_runtime.script.memory_mb == 128
    assert "run_tools_script" in ToolManager(loaded).available_tools()
