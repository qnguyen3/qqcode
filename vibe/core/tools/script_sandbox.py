from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from vibe.core.audit import AuditDecision, AuditLogger, build_audit_logger
from vibe.core.config import ScriptToolConfig, VibeConfig
from vibe.core.hooks.events import ToolCallEventHook, ToolResultEventHook
from vibe.core.llm.format import _name_matches
from vibe.core.tools.base import BaseTool, BaseToolResult, ToolError, ToolPermission
from vibe.core.types import AgentMode

if TYPE_CHECKING:
    from vibe.core.agent import Agent
    from vibe.core.hooks.registry import HookRegistry
    from vibe.core.tools.manager import ToolManager

_WORKER_PATH = Path(__file__).with_name("_script_worker.py")


class ScriptToolCall(BaseModel):
    """Metadata recorded for a single tool invocation made by a script."""

    name: str
    ok: bool
    duration: float
    error: str | None = None


class RunToolsScriptArgs(BaseModel):
    """Arguments accepted by the ``run_tools_script`` tool."""

    language: Literal["python"] = "python"
    code: str = Field(
        description="Short Python script that calls tools via `tools.<name>(**args)`."
    )
    tool_allowlist: list[str] = Field(
        default_factory=list,
        description="Explicit list of scriptable tool names this script may invoke.",
    )


class RunToolsScriptResult(BaseToolResult):
    """Structured result of running a tool script in the isolated worker."""

    output: str
    tool_calls: list[ScriptToolCall] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def format_for_model(self) -> str:
        parts: list[str] = []
        if self.output:
            parts.append(self.output.rstrip("\n"))
        elif not self.error:
            parts.append("(no output)")

        if self.truncated:
            parts.append("[output truncated: exceeded max_output_bytes]")

        if self.error:
            parts.append(f"Error: {self.error}")

        return "\n".join(parts)


class ToolBridge:
    """Mediates tool calls from the sandboxed script worker through ``ToolManager``.

    Every call is checked against ``allowed_tools``, the tool's ``scriptable``
    attribute, active agent mode/permissions, path restrictions, and hooks, and
    is recorded in the audit log.
    """

    def __init__(
        self,
        tool_manager: ToolManager,
        *,
        config: VibeConfig | None = None,
        agent: Agent | None = None,
        audit_logger: AuditLogger | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._tool_manager = tool_manager
        self._agent = agent
        self._config = (
            agent.config
            if agent is not None
            else (config if config is not None else tool_manager._config)
        )
        self._audit_logger = (
            audit_logger
            if audit_logger is not None
            else (
                agent.audit_logger
                if agent is not None
                else build_audit_logger(self._config.audit)
            )
        )
        self._hooks: HookRegistry | None = (
            hooks if hooks is not None else (agent.hooks if agent is not None else None)
        )

    @property
    def script_config(self) -> ScriptToolConfig:
        return self._config.tool_runtime.script

    def available_scriptable_tools(self) -> set[str]:
        """Return names of all registered tools that are enabled and ``scriptable``."""
        available = self._tool_manager.available_tools()
        disabled = self._config.disabled_tools
        result: set[str] = set()
        for name, tool_cls in available.items():
            if not getattr(tool_cls, "scriptable", False):
                continue
            if disabled and _name_matches(name, disabled):
                continue
            result.add(name)
        return result

    def validate_allowlist(
        self, declared: list[str], *, explicit: bool = True
    ) -> tuple[set[str], list[ScriptToolCall], str | None]:
        """Validate ``tool_allowlist`` before starting the worker subprocess."""
        scriptable = self.available_scriptable_tools()
        if not explicit:
            return scriptable, [], None

        available = self._tool_manager.available_tools()
        disabled = self._config.disabled_tools
        allowed: set[str] = set()

        for raw_name in declared:
            name = raw_name.strip()
            if not name:
                continue
            tool_cls = available.get(name)
            if tool_cls is None or (disabled and _name_matches(name, disabled)):
                reason = f"Tool '{name}' is not available"
                self._record_audit(name, {}, AuditDecision.DENIED)
                call = ScriptToolCall(name=name, ok=False, duration=0.0, error=reason)
                return set(), [call], reason

            if not getattr(tool_cls, "scriptable", False):
                reason = (
                    f"Tool '{name}' is not scriptable (scriptable=False) and "
                    "cannot be used in run_tools_script"
                )
                self._record_audit(name, {}, AuditDecision.DENIED)
                call = ScriptToolCall(name=name, ok=False, duration=0.0, error=reason)
                return set(), [call], reason

            allowed.add(name)

        return allowed & scriptable, [], None

    def check_preflight_permissions(
        self, allowed_tools: set[str], code: str
    ) -> tuple[list[ScriptToolCall], str | None]:
        """Reject unapproved tools in ``allowed_tools`` when not invoked in ``code``."""
        if self._is_auto_approve():
            return [], None

        for name in sorted(allowed_tools):
            if f"tools.{name}" in code or f'"{name}"' in code or f"'{name}'" in code:
                continue
            tool_cfg = self._tool_manager.get_tool_config(name)
            if tool_cfg.permission is ToolPermission.NEVER:
                reason = f"Tool '{name}' is permanently disabled"
                self._record_audit(name, {}, AuditDecision.DENIED)
                return [
                    ScriptToolCall(name=name, ok=False, duration=0.0, error=reason)
                ], reason
            if (
                tool_cfg.permission is ToolPermission.ASK
                and not tool_cfg.allowlist
                and not tool_cfg.path_allowlist
            ):
                reason = (
                    f"Tool '{name}' requires interactive approval (permission=ask) "
                    "and cannot be called from a script"
                )
                self._record_audit(name, {}, AuditDecision.DENIED)
                return [
                    ScriptToolCall(name=name, ok=False, duration=0.0, error=reason)
                ], reason
        return [], None

    def _validate_call_target(
        self, name: str, args: dict[str, Any], allowed_tools: set[str]
    ) -> str | None:
        available = self._tool_manager.available_tools()
        tool_cls = available.get(name)
        if tool_cls is None or (
            self._config.disabled_tools
            and _name_matches(name, self._config.disabled_tools)
        ):
            return f"Unknown or disabled tool '{name}'"
        if not getattr(tool_cls, "scriptable", False):
            return (
                f"Tool '{name}' is not scriptable (scriptable=False) and "
                "cannot be called from a script"
            )
        if name not in allowed_tools:
            return f"Tool '{name}' is not in tool_allowlist"
        return None

    async def call_tool(
        self,
        name: str,
        raw_args: dict[str, Any],
        *,
        positional: list[Any] | None = None,
        allowed_tools: set[str],
    ) -> tuple[dict[str, Any], ScriptToolCall]:
        """Validate, permission-check, audit, and invoke a single tool call."""
        start = time.perf_counter()
        tool_cls = self._tool_manager.available_tools().get(name)
        args = self._merge_positional_args(tool_cls, raw_args, positional or [])

        if (
            target_err := self._validate_call_target(name, args, allowed_tools)
        ) is not None:
            self._record_audit(name, args, AuditDecision.DENIED)
            return self._fail(name, start, target_err)

        try:
            tool = self._get_tool_instance(name)
        except Exception as exc:
            reason = f"Error instantiating tool '{name}': {exc}"
            self._record_audit(name, args, AuditDecision.DENIED)
            return self._fail(name, start, reason)

        call_id = f"script_{uuid4().hex[:12]}"
        hook_ok, args, hook_err = await self._dispatch_before_hook(name, call_id, args)
        if not hook_ok:
            reason = hook_err or f"Tool call blocked by hook: {name}"
            self._record_audit(name, args, AuditDecision.DENIED)
            await self._dispatch_after_hook(name, call_id, ok=False, error=reason)
            return self._fail(name, start, reason)

        validated_or_err = await self._validate_and_authorize(tool, args, call_id)
        if isinstance(validated_or_err, str):
            return self._fail(name, start, validated_or_err)

        return await self._execute_tool(tool, validated_or_err, call_id, start)

    async def _validate_and_authorize(
        self, tool: BaseTool, args: dict[str, Any], call_id: str
    ) -> BaseModel | str:
        name = tool.get_name()
        try:
            args_model, _ = tool._get_tool_args_results()
            validated_args = args_model.model_validate(args)
        except ValidationError as exc:
            reason = f"Validation error in tool {name}: {exc}"
            await self._dispatch_after_hook(name, call_id, ok=False, error=reason)
            return reason

        permitted, perm_reason = self._check_permission(tool, validated_args, args)
        if not permitted:
            reason = perm_reason or f"Permission denied for tool '{name}'"
            await self._dispatch_after_hook(name, call_id, ok=False, error=reason)
            return reason

        return validated_args

    def _get_tool_instance(self, name: str) -> BaseTool:
        if self._agent is not None:
            return self._agent._get_tool_instance(name)
        return self._tool_manager.get(name)

    @staticmethod
    def _merge_positional_args(
        tool_cls: type[BaseTool] | None, raw_args: dict[str, Any], positional: list[Any]
    ) -> dict[str, Any]:
        args = dict(raw_args)
        if not positional:
            return args
        if len(positional) == 1 and isinstance(positional[0], dict) and not args:
            return dict(positional[0])
        if tool_cls is None:
            return args
        try:
            args_model, _ = tool_cls._get_tool_args_results()
            field_names = list(args_model.model_fields.keys())
            for index, value in enumerate(positional):
                if index < len(field_names) and field_names[index] not in args:
                    args[field_names[index]] = value
        except Exception:
            pass
        return args

    async def _dispatch_before_hook(
        self, name: str, call_id: str, args: dict[str, Any]
    ) -> tuple[bool, dict[str, Any], str | None]:
        if self._hooks is None or not self._hooks.has_handlers:
            return True, args, None
        decision = await self._hooks.dispatch(
            ToolCallEventHook(tool_name=name, tool_call_id=call_id, args=dict(args))
        )
        match decision.action:
            case "deny":
                return (
                    False,
                    args,
                    decision.reason or f"Tool call blocked by hook: {name}",
                )
            case "modify" if decision.replacement_args is not None:
                return True, dict(decision.replacement_args), None
            case _:
                return True, args, None

    async def _dispatch_after_hook(
        self,
        name: str,
        call_id: str,
        *,
        ok: bool,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._hooks is None or not self._hooks.has_handlers:
            return
        await self._hooks.dispatch(
            ToolResultEventHook(
                tool_name=name, tool_call_id=call_id, ok=ok, text=text, error=error
            )
        )

    def _is_auto_approve(self) -> bool:
        return self._agent is not None and self._agent.mode == AgentMode.AUTO_APPROVE

    def _check_permission(
        self, tool: BaseTool, validated_args: BaseModel, raw_args: dict[str, Any]
    ) -> tuple[bool, str | None]:
        tool_name = tool.get_name()

        path_decision = tool.check_path_restrictions(validated_args)
        if path_decision is ToolPermission.NEVER:
            self._record_audit(tool_name, raw_args, AuditDecision.DENYLIST)
            return (
                False,
                f"Tool '{tool_name}' blocked by path restrictions "
                "(path_allowlist/path_denylist).",
            )

        if self._agent is not None and self._agent.mode == AgentMode.PLAN:
            from vibe.core.agent import PLAN_MODE_ALLOWED_TOOLS

            if tool_name not in PLAN_MODE_ALLOWED_TOOLS:
                self._record_audit(tool_name, raw_args, AuditDecision.DENIED)
                return False, f"Plan mode: '{tool_name}' is not allowed."

        allowlist_denylist = tool.check_allowlist_denylist(validated_args)
        if allowlist_denylist is ToolPermission.NEVER:
            self._record_audit(tool_name, raw_args, AuditDecision.DENYLIST)
            patterns = ", ".join(repr(p) for p in tool.config.denylist)
            return False, f"Tool '{tool_name}' blocked by denylist: [{patterns}]"

        perm = self._tool_manager.get_tool_config(tool_name).permission
        if perm is ToolPermission.NEVER:
            self._record_audit(tool_name, raw_args, AuditDecision.DENIED)
            return False, f"Tool '{tool_name}' is permanently disabled"

        if (
            self._is_auto_approve()
            or allowlist_denylist is ToolPermission.ALWAYS
            or path_decision is ToolPermission.ALWAYS
            or perm is ToolPermission.ALWAYS
        ):
            self._record_audit(tool_name, raw_args, AuditDecision.APPROVED)
            return True, None

        self._record_audit(tool_name, raw_args, AuditDecision.DENIED)
        return (
            False,
            f"Tool '{tool_name}' requires interactive approval (permission=ask) "
            "and cannot be called from a script",
        )

    async def _execute_tool(
        self, tool: BaseTool, validated_args: BaseModel, call_id: str, start: float
    ) -> tuple[dict[str, Any], ScriptToolCall]:
        name = tool.get_name()
        timeout = tool.config.timeout
        try:
            if timeout is not None and timeout > 0:
                result_model = await asyncio.wait_for(
                    tool.run(validated_args), timeout=timeout
                )
            else:
                result_model = await tool.run(validated_args)
        except TimeoutError:
            reason = f"Tool '{name}' timed out after {timeout}s"
            await self._dispatch_after_hook(name, call_id, ok=False, error=reason)
            return self._fail(name, start, reason)
        except (ToolError, Exception) as exc:
            reason = str(exc)
            await self._dispatch_after_hook(name, call_id, ok=False, error=reason)
            return self._fail(name, start, reason)

        duration = time.perf_counter() - start
        payload = result_model.model_dump(mode="json")
        formatter = getattr(result_model, "format_for_model", None)
        formatted = str(formatter()) if callable(formatter) else None
        if formatted is not None and "formatted" not in payload:
            payload["formatted"] = formatted

        await self._dispatch_after_hook(name, call_id, ok=True, text=formatted)
        call_record = ScriptToolCall(name=name, ok=True, duration=duration)
        return {"ok": True, "result": payload}, call_record

    def _record_audit(
        self, tool_name: str, args: dict[str, Any], decision: AuditDecision
    ) -> None:
        if self._audit_logger is None:
            return
        mode = self._agent.mode.value if self._agent is not None else "script"
        session_id = self._agent.session_id if self._agent is not None else None
        self._audit_logger.record(
            tool_name, args, decision, mode=mode, session_id=session_id
        )

    @staticmethod
    def _fail(
        name: str, start: float, reason: str
    ) -> tuple[dict[str, Any], ScriptToolCall]:
        duration = time.perf_counter() - start
        return (
            {"ok": False, "error": reason},
            ScriptToolCall(name=name, ok=False, duration=duration, error=reason),
        )


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if hasattr(os, "killpg") and proc.pid is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with suppress(ProcessLookupError, OSError):
        proc.kill()


class _WorkerAccumulator:
    """Mutable state collected while communicating with the worker subprocess."""

    def __init__(self, max_output_bytes: int, max_calls: int) -> None:
        self.max_output_bytes = max_output_bytes
        self.max_calls = max_calls
        self.output_chunks: list[str] = []
        self.output_bytes = 0
        self.truncated = False
        self.tool_calls: list[ScriptToolCall] = []
        self.calls_made = 0
        self.first_call_error: str | None = None
        self.worker_error: str | None = None
        self.done_received = False

    def append_output(self, chunk: str) -> None:
        encoded = chunk.encode("utf-8", errors="replace")
        remaining = self.max_output_bytes - self.output_bytes
        if len(encoded) <= remaining:
            self.output_chunks.append(chunk)
            self.output_bytes += len(encoded)
            return
        if remaining > 0:
            clipped = encoded[:remaining].decode("utf-8", errors="ignore")
            if clipped:
                self.output_chunks.append(clipped)
                self.output_bytes += len(clipped.encode("utf-8"))
        self.truncated = True

    def build_result(
        self, *, override_error: str | None = None
    ) -> RunToolsScriptResult:
        error = override_error or self.worker_error or self.first_call_error
        return RunToolsScriptResult(
            output="".join(self.output_chunks),
            tool_calls=self.tool_calls,
            truncated=self.truncated,
            error=error,
        )


class ScriptSandbox:
    """Runs Python tool scripts inside an isolated subprocess with resource caps."""

    def __init__(self, *, memory_mb: int = 256) -> None:
        self.memory_mb = memory_mb

    async def run(
        self,
        code: str,
        *,
        allowed_tools: set[str],
        bridge: ToolBridge,
        timeout: float = 30.0,
        max_calls: int = 20,
        max_output_bytes: int = 16384,
        memory_mb: int | None = None,
    ) -> RunToolsScriptResult:
        effective_memory_mb = memory_mb if memory_mb is not None else self.memory_mb
        stream_limit = max(1024 * 1024, max_output_bytes * 4)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(_WORKER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PYTHONIOENCODING": "utf-8"},
            start_new_session=True,
            limit=stream_limit,
        )

        acc = _WorkerAccumulator(max_output_bytes=max_output_bytes, max_calls=max_calls)

        try:
            async with asyncio.timeout(timeout):
                await self._communicate_with_worker(
                    proc,
                    code=code,
                    allowed_tools=allowed_tools,
                    bridge=bridge,
                    memory_mb=effective_memory_mb,
                    acc=acc,
                )
        except TimeoutError:
            _kill_process(proc)
            await proc.wait()
            return acc.build_result(
                override_error=f"Script timed out after {timeout:g}s"
            )
        finally:
            if proc.returncode is None:
                _kill_process(proc)
                await proc.wait()

        return acc.build_result()

    async def _communicate_with_worker(
        self,
        proc: asyncio.subprocess.Process,
        *,
        code: str,
        allowed_tools: set[str],
        bridge: ToolBridge,
        memory_mb: int,
        acc: _WorkerAccumulator,
    ) -> None:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        init_payload = json.dumps({
            "code": code,
            "allowed_tools": sorted(allowed_tools),
            "max_calls": acc.max_calls,
            "max_output_bytes": acc.max_output_bytes,
            "memory_mb": memory_mb,
        })
        proc.stdin.write(f"{init_payload}\n".encode())
        await proc.stdin.drain()

        while raw_line := await proc.stdout.readline():
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            if await self._handle_worker_message(
                proc.stdin, message, bridge, allowed_tools, acc
            ):
                break

        stderr_bytes = await proc.stderr.read()
        await proc.wait()
        self._finalize_worker_status(proc.returncode, stderr_bytes, acc)

    async def _handle_worker_message(
        self,
        stdin: asyncio.StreamWriter,
        message: dict[str, Any],
        bridge: ToolBridge,
        allowed_tools: set[str],
        acc: _WorkerAccumulator,
    ) -> bool:
        """Process one JSON message from the worker; returns True when done."""
        match message.get("type"):
            case "output":
                acc.append_output(str(message.get("data", "")))
                return False
            case "call":
                reply = await self._handle_call_request(
                    message, bridge, allowed_tools, acc
                )
                stdin.write(f"{json.dumps(reply)}\n".encode())
                await stdin.drain()
                return False
            case "done":
                acc.done_received = True
                acc.truncated = acc.truncated or bool(message.get("truncated", False))
                raw_err = message.get("error")
                acc.worker_error = str(raw_err) if raw_err else None
                return True
            case _:
                return False

    @staticmethod
    async def _handle_call_request(
        message: dict[str, Any],
        bridge: ToolBridge,
        allowed_tools: set[str],
        acc: _WorkerAccumulator,
    ) -> dict[str, Any]:
        tool_name = str(message.get("tool", ""))
        raw_args = message.get("args")
        args_dict = dict(raw_args) if isinstance(raw_args, dict) else {}
        pos_raw = message.get("positional")
        positional = list(pos_raw) if isinstance(pos_raw, list) else []

        if acc.calls_made >= acc.max_calls:
            limit_error = f"Script exceeded max_calls limit ({acc.max_calls})"
            acc.tool_calls.append(
                ScriptToolCall(
                    name=tool_name, ok=False, duration=0.0, error=limit_error
                )
            )
            acc.first_call_error = acc.first_call_error or limit_error
            return {"ok": False, "error": limit_error, "abort": True}

        acc.calls_made += 1
        response, call_record = await bridge.call_tool(
            tool_name, args_dict, positional=positional, allowed_tools=allowed_tools
        )
        acc.tool_calls.append(call_record)
        if not call_record.ok and acc.first_call_error is None:
            acc.first_call_error = call_record.error
        return response

    @staticmethod
    def _finalize_worker_status(
        returncode: int | None, stderr_bytes: bytes, acc: _WorkerAccumulator
    ) -> None:
        if acc.done_received or acc.worker_error is not None:
            return
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if "MemoryError" in stderr_text or returncode in {-9, -11, 137, 139}:
            acc.worker_error = "MemoryError: Script exceeded memory limit"
        elif stderr_text:
            acc.worker_error = f"Worker failed: {stderr_text.splitlines()[-1]}"
        elif returncode not in {0, None}:
            acc.worker_error = f"Worker exited with code {returncode}"
