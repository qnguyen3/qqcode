from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, final

from pydantic import ConfigDict, Field

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState, ToolPermission
from vibe.core.tools.script_sandbox import (
    RunToolsScriptArgs,
    RunToolsScriptResult,
    ScriptSandbox,
    ScriptToolCall,
    ToolBridge,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData

if TYPE_CHECKING:
    from vibe.core.types import ToolCallEvent, ToolResultEvent

__all__ = [
    "RunToolsScript",
    "RunToolsScriptArgs",
    "RunToolsScriptConfig",
    "RunToolsScriptResult",
    "RunToolsScriptState",
    "ScriptToolCall",
]


class RunToolsScriptConfig(BaseToolConfig):
    """Configuration for the ``run_tools_script`` tool."""

    permission: ToolPermission = ToolPermission.ALWAYS
    timeout: float | None = Field(
        default=None, description="Per-script timeout override in seconds."
    )
    max_calls: int | None = Field(
        default=None, description="Maximum number of tool calls per script."
    )
    max_output_bytes: int | None = Field(
        default=None, description="Maximum stdout bytes captured from a script."
    )
    memory_mb: int | None = Field(
        default=None, description="Virtual memory cap (RLIMIT_AS) in megabytes."
    )


class RunToolsScriptState(BaseToolState):
    model_config = ConfigDict(
        arbitrary_types_allowed=True, validate_default=True, extra="forbid"
    )
    bridge: ToolBridge | None = Field(default=None, exclude=True)


class RunToolsScript(
    BaseTool[
        RunToolsScriptArgs,
        RunToolsScriptResult,
        RunToolsScriptConfig,
        RunToolsScriptState,
    ],
    ToolUIData[RunToolsScriptArgs, RunToolsScriptResult],
):
    description: ClassVar[str] = (
        "Execute a short Python script in an isolated subprocess that can "
        "orchestrate multiple read-only tools via `tools.<name>(**args)` and "
        "print a compact summary."
    )
    scriptable: ClassVar[bool] = False

    def attach_bridge(self, bridge: ToolBridge) -> None:
        """Inject the bridge used to mediate script tool calls."""
        self.state.bridge = bridge

    def _get_or_create_bridge(self) -> ToolBridge:
        if self.state.bridge is not None:
            return self.state.bridge

        from vibe.core.config import (
            ScriptToolConfig,
            SessionLoggingConfig,
            ToolRuntimeConfig,
            VibeConfig,
            skip_api_key_check,
        )
        from vibe.core.tools.manager import ToolManager

        with skip_api_key_check():
            fallback_config = VibeConfig(
                session_logging=SessionLoggingConfig(enabled=False),
                workdir=self.config.effective_workdir,
                tool_runtime=ToolRuntimeConfig(script=ScriptToolConfig(enabled=True)),
            )
        manager = ToolManager(fallback_config)
        self.state.bridge = ToolBridge(manager, config=fallback_config)
        return self.state.bridge

    @final
    async def run(self, args: RunToolsScriptArgs) -> RunToolsScriptResult:
        bridge = self._get_or_create_bridge()
        explicit_allowlist = "tool_allowlist" in args.model_fields_set

        allowed_tools, failed_calls, allowlist_err = bridge.validate_allowlist(
            args.tool_allowlist, explicit=explicit_allowlist
        )
        if allowlist_err is not None:
            return RunToolsScriptResult(
                output="", tool_calls=failed_calls, truncated=False, error=allowlist_err
            )

        preflight_calls, preflight_err = bridge.check_preflight_permissions(
            allowed_tools, args.code
        )
        if preflight_err is not None:
            return RunToolsScriptResult(
                output="",
                tool_calls=preflight_calls,
                truncated=False,
                error=preflight_err,
            )

        script_cfg = bridge.script_config
        timeout = (
            self.config.timeout
            if self.config.timeout is not None
            else script_cfg.timeout
        )
        max_calls = (
            self.config.max_calls
            if self.config.max_calls is not None
            else script_cfg.max_calls
        )
        max_output_bytes = (
            self.config.max_output_bytes
            if self.config.max_output_bytes is not None
            else script_cfg.max_output_bytes
        )
        memory_mb = (
            self.config.memory_mb
            if self.config.memory_mb is not None
            else script_cfg.memory_mb
        )

        sandbox = ScriptSandbox(memory_mb=memory_mb)
        return await sandbox.run(
            args.code,
            allowed_tools=allowed_tools,
            bridge=bridge,
            timeout=timeout,
            max_calls=max_calls,
            max_output_bytes=max_output_bytes,
            memory_mb=memory_mb,
        )

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        allowlist = getattr(event.args, "tool_allowlist", []) or []
        tools_str = ", ".join(allowlist) if allowlist else "read-only"
        return ToolCallDisplay(
            summary=f"run_tools_script ({tools_str})",
            details={
                "language": getattr(event.args, "language", "python"),
                "tool_allowlist": list(allowlist),
                "code": getattr(event.args, "code", ""),
            },
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if event.error:
            return ToolResultDisplay(success=False, message=event.error)

        result = event.result
        if result is None:
            return ToolResultDisplay(
                success=False, message=event.skip_reason or "No result"
            )

        error = getattr(result, "error", None)
        tool_calls = getattr(result, "tool_calls", []) or []
        output = str(getattr(result, "output", ""))
        truncated = bool(getattr(result, "truncated", False))
        details = {
            "tool_calls": len(tool_calls),
            "truncated": truncated,
            "output": output,
        }
        if error:
            return ToolResultDisplay(success=False, message=str(error), details=details)
        return ToolResultDisplay(
            success=True,
            message=f"Ran script ({len(tool_calls)} tool call(s))",
            details=details,
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Running tool script"


if __name__ != "vibe.core.tools.builtins.run_tools_script":
    import importlib

    _canonical = importlib.import_module("vibe.core.tools.builtins.run_tools_script")
    RunToolsScript = _canonical.RunToolsScript
    RunToolsScriptConfig = _canonical.RunToolsScriptConfig
    RunToolsScriptState = _canonical.RunToolsScriptState
