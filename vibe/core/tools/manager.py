from __future__ import annotations

from collections.abc import Callable, Iterator
from fnmatch import fnmatch
import importlib.util
import inspect
from logging import getLogger
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any

from vibe import VIBE_ROOT
from vibe.core.config import get_vibe_home
from vibe.core.packages import package_resource_dirs
from vibe.core.tools.base import BaseTool, BaseToolConfig, ToolExposure
from vibe.core.tools.file_mutation_queue import FileMutationQueue
from vibe.core.tools.mcp import (
    RemoteTool,
    create_mcp_http_proxy_tool_class,
    create_mcp_stdio_proxy_tool_class,
    list_tools_http,
    list_tools_stdio,
)
from vibe.core.tools.search import TOOL_SEARCH_TOOL_NAME
from vibe.core.utils import run_sync

logger = getLogger("vibe")

if TYPE_CHECKING:
    from vibe.core.config import MCPHttp, MCPStdio, MCPStreamableHttp, VibeConfig
    from vibe.core.mcp.manager import MCPManager
    from vibe.core.skills.manager import SkillManager


class NoSuchToolError(Exception):
    """Exception raised when a tool is not found."""


DEFAULT_TOOL_DIR = VIBE_ROOT / "core" / "tools" / "builtins"


class ToolManager:
    """Manages tool discovery and instantiation for an Agent.

    Discovers available tools from the provided search paths. Each Agent
    should have its own ToolManager instance.
    """

    def __init__(
        self,
        config: VibeConfig,
        skill_manager: SkillManager | None = None,
        mcp_manager: MCPManager | None = None,
        tools_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._skill_manager = skill_manager
        self._mcp_manager = mcp_manager
        self._tools_changed_callback = tools_changed_callback
        self._mcp_tool_names: set[str] = set()
        self._exposure: dict[str, ToolExposure] = {}
        self._exposed: set[str] = set()
        self._instances: dict[str, BaseTool] = {}
        self._search_paths: list[Path] = self._compute_search_paths(config)
        self.file_mutation_queue = FileMutationQueue()

        self._available: dict[str, type[BaseTool]] = {}
        for cls in self._iter_tool_classes(self._search_paths):
            name = cls.get_name()
            if (
                name == "run_tools_script"
                and not self._config.tool_runtime.script.enabled
            ):
                continue
            if name in self._available:
                logger.warning(
                    "Duplicate tool '%s' from %s overrides an earlier definition",
                    name,
                    cls.__module__,
                )
            self._available[name] = cls
        if mcp_manager is not None:
            self._apply_mcp_tools()
            mcp_manager.on_tools_changed(self._apply_mcp_tools)
        else:
            # Compatibility shim: connect-per-call discovery when no manager
            # is provided by the caller.
            self._integrate_mcp()
        self._recompute_exposure()

    @staticmethod
    def _compute_search_paths(config: VibeConfig) -> list[Path]:
        """Compute tool search paths in increasing priority order.

        Later paths win on name collisions: project > package > global >
        explicit ``tool_paths`` > builtins.
        """
        paths: list[Path] = [DEFAULT_TOOL_DIR]

        for p in config.tool_paths:
            path = Path(p).expanduser().resolve()
            if path.is_dir():
                paths.append(path)

        global_tools = get_vibe_home() / "tools"
        if global_tools.is_dir():
            paths.append(global_tools)

        paths.extend(package_resource_dirs(config.packages, "tools"))

        if config.project_trusted:
            cwd = config.effective_workdir
            for directory in (cwd, *cwd.parents):
                tools_dir = directory / ".qqcode" / "tools"
                if tools_dir.is_dir():
                    paths.append(tools_dir)
                    break
        else:
            logger.debug(
                "Skipping project tool discovery in %s: project is not trusted",
                config.effective_workdir,
            )

        unique: list[Path] = []
        seen: set[Path] = set()
        for p in paths:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                unique.append(rp)
        return unique

    @staticmethod
    def _iter_tool_classes(search_paths: list[Path]) -> Iterator[type[BaseTool]]:
        for base in search_paths:
            if not base.is_dir():
                continue

            for path in base.rglob("*.py"):
                if not path.is_file():
                    continue
                name = path.name
                if name.startswith("_"):
                    continue

                stem = re.sub(r"[^0-9A-Za-z_]", "_", path.stem) or "mod"
                module_name = f"vibe_tools_discovered_{stem}"

                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    continue

                for obj in vars(module).values():
                    if not inspect.isclass(obj):
                        continue
                    if not issubclass(obj, BaseTool) or obj is BaseTool:
                        continue
                    if inspect.isabstract(obj):
                        continue
                    yield obj

    @staticmethod
    def discover_tool_defaults(
        search_paths: list[Path] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if search_paths is None:
            search_paths = [DEFAULT_TOOL_DIR]

        defaults: dict[str, dict[str, Any]] = {}
        for cls in ToolManager._iter_tool_classes(search_paths):
            try:
                tool_name = cls.get_name()
                config_class = cls._get_tool_config_class()
                defaults[tool_name] = config_class().model_dump(exclude_none=True)
            except Exception as e:
                logger.warning(
                    "Failed to get defaults for tool %s: %s", cls.__name__, e
                )
                continue
        return defaults

    def available_tools(self) -> dict[str, type[BaseTool]]:
        return dict(self._available)

    def exposure_for(self, name: str) -> ToolExposure:
        """Return the resolved exposure for a registered tool."""
        return self._exposure.get(name, "direct")

    def deferred_pool(self) -> dict[str, str]:
        """Deferred tools available to ``tool_search``, keyed by name."""
        return {
            name: (tool_class.description or "")
            for name, tool_class in self._available.items()
            if self._exposure.get(name) == "deferred"
        }

    def tool_search_class(self) -> type[BaseTool] | None:
        """The ``tool_search`` builtin when at least one tool is deferred."""
        if not self.deferred_pool():
            return None
        return self._available.get(TOOL_SEARCH_TOOL_NAME)

    def expose(self, names: list[str]) -> list[str]:
        """Expose deferred tools for the current turn (one-shot)."""
        pool = self.deferred_pool()
        exposed = [name for name in names if name in pool]
        self._exposed.update(exposed)
        return exposed

    def model_visible_tools(self) -> list[type[BaseTool]]:
        """Tool classes whose schemas may be sent to the model."""
        visible: list[type[BaseTool]] = []
        for name, tool_class in self._available.items():
            match self._exposure.get(name, "direct"):
                case "hidden":
                    continue
                case "deferred":
                    if name in self._exposed:
                        visible.append(tool_class)
                case _:
                    visible.append(tool_class)
        return visible

    def reset_deferred_exposure(self) -> None:
        """Expire one-shot exposure of deferred tools."""
        self._exposed.clear()
        if self._mcp_manager is not None:
            self._mcp_manager.reset_deferred_activation()

    def apply_mcp_tools(self) -> None:
        """Re-sync MCP tool classes from the manager (also used as a callback)."""
        self._apply_mcp_tools()

    def _apply_mcp_tools(self) -> None:
        manager = self._mcp_manager
        if manager is None:
            return
        classes = {cls.get_name(): cls for cls in manager.registered_tool_classes()}
        changed = False

        for name in list(self._mcp_tool_names):
            if name not in classes:
                self._available.pop(name, None)
                self._instances.pop(name, None)
                self._mcp_tool_names.discard(name)
                changed = True

        for name, tool_class in classes.items():
            if self._available.get(name) is not tool_class:
                self._available[name] = tool_class
                self._instances.pop(name, None)
                self._mcp_tool_names.add(name)
                changed = True

        self._recompute_exposure()
        if changed and self._tools_changed_callback is not None:
            self._tools_changed_callback()

    def _config_exposure(self, name: str) -> ToolExposure | None:
        """Resolve ``[tools.exposure]`` globs; the last match wins."""
        resolved: ToolExposure | None = None
        for pattern, exposure in self._config.tool_runtime.exposure.items():
            if fnmatch(name, pattern):
                resolved = exposure
        return resolved

    def _recompute_exposure(self) -> None:
        """Resolve the exposure of every registered tool.

        Priority: ``[tools.exposure]`` overrides, MCP server exposure, then the
        tool class default. Large MCP tool sets are deferred automatically.
        """
        hidden = self._mcp_manager.hidden_tool_names() if self._mcp_manager else set()
        mcp_direct = [
            name
            for name in self._mcp_tool_names
            if name not in hidden
            and self._config_exposure(name) is None
            and self._available[name].exposure in {"direct", "model_only"}
        ]
        auto_defer = len(mcp_direct) > self._config.tool_runtime.defer_when_more_than

        self._exposure = {
            name: self._resolve_exposure(
                name,
                hidden=name in hidden,
                auto_defer=auto_defer and name in mcp_direct,
            )
            for name in self._available
        }
        if not self.deferred_pool():
            self._exposed.clear()
        if TOOL_SEARCH_TOOL_NAME in self._available:
            self._exposure[TOOL_SEARCH_TOOL_NAME] = (
                "direct" if self.deferred_pool() else "hidden"
            )

    def _resolve_exposure(
        self, name: str, *, hidden: bool, auto_defer: bool
    ) -> ToolExposure:
        if (override := self._config_exposure(name)) is not None:
            return override
        if hidden:
            return "hidden"
        if auto_defer:
            return "deferred"
        return self._available[name].exposure

    def _integrate_mcp(self) -> None:
        if not self._config.mcp_servers:
            return
        run_sync(self._integrate_mcp_async())

    async def _integrate_mcp_async(self) -> None:
        try:
            http_count = 0
            stdio_count = 0

            for srv in self._config.mcp_servers:
                match srv.transport:
                    case "http" | "streamable-http":
                        http_count += await self._register_http_server(srv)
                    case "stdio":
                        stdio_count += await self._register_stdio_server(srv)
                    case _:
                        logger.warning("Unsupported MCP transport: %r", srv.transport)

            logger.info(
                "MCP integration registered %d tools (http=%d, stdio=%d)",
                http_count + stdio_count,
                http_count,
                stdio_count,
            )
        except Exception as exc:
            logger.warning("Failed to integrate MCP tools: %s", exc)

    async def _register_http_server(self, srv: MCPHttp | MCPStreamableHttp) -> int:
        url = (srv.url or "").strip()
        if not url:
            logger.warning("MCP server '%s' missing url for http transport", srv.name)
            return 0

        headers = srv.http_headers()
        try:
            tools: list[RemoteTool] = await list_tools_http(url, headers=headers)
        except Exception as exc:
            logger.warning("MCP HTTP discovery failed for %s: %s", url, exc)
            return 0

        added = 0
        for remote in tools:
            try:
                proxy_cls = create_mcp_http_proxy_tool_class(
                    url=url,
                    remote=remote,
                    alias=srv.name,
                    server_hint=srv.prompt,
                    headers=headers,
                )
                self._available[proxy_cls.get_name()] = proxy_cls
                added += 1
            except Exception as exc:
                logger.warning(
                    "Failed to register MCP HTTP tool '%s' from %s: %r",
                    getattr(remote, "name", "<unknown>"),
                    url,
                    exc,
                )
        return added

    async def _register_stdio_server(self, srv: MCPStdio) -> int:
        cmd = srv.argv()
        if not cmd:
            logger.warning("MCP stdio server '%s' has invalid/empty command", srv.name)
            return 0

        try:
            tools: list[RemoteTool] = await list_tools_stdio(cmd)
        except Exception as exc:
            logger.warning("MCP stdio discovery failed for %r: %s", cmd, exc)
            return 0

        added = 0
        for remote in tools:
            try:
                proxy_cls = create_mcp_stdio_proxy_tool_class(
                    command=cmd, remote=remote, alias=srv.name, server_hint=srv.prompt
                )
                self._available[proxy_cls.get_name()] = proxy_cls
                added += 1
            except Exception as exc:
                logger.warning(
                    "Failed to register MCP stdio tool '%s' from %r: %r",
                    getattr(remote, "name", "<unknown>"),
                    cmd,
                    exc,
                )
        return added

    def get_tool_config(self, tool_name: str) -> BaseToolConfig:
        tool_class = self._available.get(tool_name)

        if tool_class:
            config_class = tool_class._get_tool_config_class()
            default_config = config_class()
        else:
            config_class = BaseToolConfig
            default_config = BaseToolConfig()

        user_overrides = self._config.tools.get(tool_name)
        if user_overrides is None:
            merged_dict = default_config.model_dump()
        else:
            merged_dict = {
                **default_config.model_dump(),
                **user_overrides.model_dump(exclude_unset=True),
            }

        if self._config.workdir is not None:
            merged_dict["workdir"] = self._config.workdir

        return config_class.model_validate(merged_dict)

    def get(self, tool_name: str) -> BaseTool:
        """Get a tool instance, creating it lazily on first call.

        Raises:
            NoSuchToolError: If the requested tool is not available.
        """
        if tool_name in self._instances:
            return self._instances[tool_name]

        if tool_name not in self._available:
            raise NoSuchToolError(
                f"Unknown tool: {tool_name}. Available: {list(self._available.keys())}"
            )

        tool_class = self._available[tool_name]
        tool_config = self.get_tool_config(tool_name)

        # Special handling for the Skill tool - inject SkillManager
        if tool_name == "skill" and self._skill_manager is not None:
            from vibe.core.tools.builtins.skill import Skill, SkillConfig

            if isinstance(tool_config, SkillConfig):
                instance = Skill.create_with_skill_manager(
                    tool_config, self._skill_manager
                )
                instance.state.file_mutation_queue = self.file_mutation_queue
                self._instances[tool_name] = instance
                return self._instances[tool_name]

        # Special handling for tool_search - it reads the deferred tool pool
        if tool_name == TOOL_SEARCH_TOOL_NAME:
            from vibe.core.tools.builtins.tool_search import ToolSearch

            search = ToolSearch.create_with_manager(tool_config, self)
            search.state.file_mutation_queue = self.file_mutation_queue
            self._instances[tool_name] = search
            return self._instances[tool_name]

        instance = tool_class.from_config(tool_config)
        instance.state.file_mutation_queue = self.file_mutation_queue
        if tool_name == "run_tools_script":
            from vibe.core.tools.script_sandbox import ToolBridge

            attach_bridge = getattr(instance, "attach_bridge", None)
            if callable(attach_bridge):
                attach_bridge(ToolBridge(self, config=self._config))
        self._instances[tool_name] = instance
        return self._instances[tool_name]

    def reset_all(self) -> None:
        self._instances.clear()
