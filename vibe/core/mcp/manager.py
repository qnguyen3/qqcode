from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from pydantic import BaseModel

from vibe.core.config import MCPServer, VibeConfig
from vibe.core.mcp.client import MCPServerClient, server_display_target
from vibe.core.mcp.exposure import (
    MCP_SEARCH_TOOL_NAME,
    DeferredToolRegistry,
    create_mcp_search_tool_class,
)
from vibe.core.mcp.oauth import MCPOAuthTokenStore
from vibe.core.mcp.protocol import RemoteTool
from vibe.core.mcp.resources import create_resource_tool_classes
from vibe.core.tools.base import BaseTool
from vibe.core.tools.mcp import create_mcp_client_proxy_tool_class

logger = logging.getLogger(__name__)


class MCPServerStatus(BaseModel):
    name: str
    transport: str
    target: str
    enabled: bool
    connected: bool
    exposure: str
    tools: int | None = None
    error: str | None = None


class MCPManager:
    """Owns one ``MCPServerClient`` per configured server.

    Builds proxy/resource tool classes for ``ToolManager``, caches per-server
    tool lists and refreshes them when servers report
    ``notifications/tools/list_changed``.
    """

    def __init__(
        self, config: VibeConfig, *, token_store: MCPOAuthTokenStore | None = None
    ) -> None:
        self.config = config
        self._servers: dict[str, MCPServer] = {}
        self._clients: dict[str, MCPServerClient] = {}
        self._server_tools: dict[
            str, dict[str, type[BaseTool[Any, Any, Any, Any]]]
        ] = {}
        self._visible: list[type[BaseTool[Any, Any, Any, Any]]] = []
        self._hidden: dict[str, type[BaseTool[Any, Any, Any, Any]]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._registry = DeferredToolRegistry()
        self._search_tool_class = create_mcp_search_tool_class(
            self._registry, activate=self.activate
        )
        self._started = False
        store = token_store or MCPOAuthTokenStore()

        for server in config.mcp_servers:
            self._servers[server.name] = server
            if not server.enabled:
                continue
            client = MCPServerClient(server, token_store=store)
            client.on_tools_changed(self._make_change_handler(server.name))
            self._clients[server.name] = client

    @property
    def clients(self) -> dict[str, MCPServerClient]:
        return dict(self._clients)

    @property
    def enabled_servers(self) -> list[MCPServer]:
        return [server for server in self.config.mcp_servers if server.enabled]

    def on_tools_changed(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    async def start(self) -> None:
        """Connect every enabled server and build the initial tool cache."""
        results = await asyncio.gather(
            *(client.start() for client in self._clients.values()),
            return_exceptions=True,
        )
        for client, result in zip(self._clients.values(), results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "MCP server '%s' unavailable at startup: %s", client.name, result
                )
        await self.refresh()
        self._started = True
        self._notify()

    async def stop(self) -> None:
        for task in list(self._refresh_tasks.values()):
            task.cancel()
        self._refresh_tasks.clear()
        for client in self._clients.values():
            try:
                await client.stop()
            except Exception:
                logger.exception("Error stopping MCP server '%s'", client.name)
        self._started = False

    async def tool_classes(self) -> list[type[BaseTool[Any, Any, Any, Any]]]:
        return self.cached_tool_classes()

    def cached_tool_classes(self) -> list[type[BaseTool[Any, Any, Any, Any]]]:
        """Tool classes eligible for the model's tool schema."""
        return list(self._visible)

    def registered_tool_classes(self) -> list[type[BaseTool[Any, Any, Any, Any]]]:
        """Every discovered tool class, including servers set to ``hidden``.

        Hidden tools are registered so callers can invoke them by name while
        their schemas stay out of model requests.
        """
        return [*self._visible, *self._hidden.values()]

    def hidden_tool_names(self) -> set[str]:
        return set(self._hidden)

    async def refresh(self, server_name: str | None = None) -> None:
        """Rebuild the tool cache, optionally for a single server."""
        names = [server_name] if server_name is not None else list(self._clients)
        changed = False
        for name in names:
            client = self._clients.get(name)
            server = self._servers.get(name)
            if client is None or server is None or not client.connected:
                continue
            try:
                tools = await client.list_tools(refresh=True)
            except Exception as exc:
                logger.warning("MCP discovery failed for '%s': %s", name, exc)
                continue
            self._server_tools[name] = self._build_server_tools(client, server, tools)
            changed = True
        if changed:
            self._recompute_visible()
            self._notify()

    def activate(self, names: list[str]) -> list[str]:
        activated = self._registry.activate(names)
        if activated:
            self._recompute_visible()
            self._notify()
        return activated

    def reset_deferred_activation(self) -> None:
        """Return deferred tools to their hidden state.

        Deferred exposure is one-shot: tools activated by ``mcp_search`` stay
        in the tool schema for the current turn and disappear at the start of
        the next one.
        """
        if not self._registry.active():
            return
        self._registry.reset()
        self._recompute_visible()
        self._notify()

    async def server_statuses(self) -> list[MCPServerStatus]:
        statuses: list[MCPServerStatus] = []
        for server in self.config.mcp_servers:
            client = self._clients.get(server.name)
            connected = client.connected if client is not None else False
            count: int | None = None
            error: str | None = None
            if client is not None and connected:
                try:
                    count = len(await client.list_tools())
                except Exception as exc:
                    error = str(exc)
            statuses.append(
                MCPServerStatus(
                    name=server.name,
                    transport=server.transport,
                    target=server_display_target(server),
                    enabled=server.enabled,
                    connected=connected,
                    exposure=server.tool_exposure,
                    tools=count,
                    error=error,
                )
            )
        return statuses

    def _build_server_tools(
        self, client: MCPServerClient, server: MCPServer, tools: list[RemoteTool]
    ) -> dict[str, type[BaseTool[Any, Any, Any, Any]]]:
        classes: dict[str, type[BaseTool[Any, Any, Any, Any]]] = {}
        for remote in tools:
            proxy = create_mcp_client_proxy_tool_class(
                client=client,
                remote=remote,
                alias=server.name,
                server_hint=server.prompt,
            )
            classes[proxy.get_name()] = proxy
        for resource_tool in create_resource_tool_classes(client):
            classes[resource_tool.get_name()] = resource_tool
        return classes

    def _recompute_visible(self) -> None:
        visible: dict[str, type[BaseTool[Any, Any, Any, Any]]] = {}
        deferred: dict[
            str, tuple[str, str | None, type[BaseTool[Any, Any, Any, Any]]]
        ] = {}
        hidden: dict[str, type[BaseTool[Any, Any, Any, Any]]] = {}
        for name, classes in self._server_tools.items():
            server = self._servers.get(name)
            if server is None:
                continue
            match server.tool_exposure:
                case "hidden":
                    hidden.update(classes)
                case "deferred":
                    for tool_name, tool_class in classes.items():
                        deferred[tool_name] = (
                            server.name,
                            getattr(tool_class, "description", None),
                            tool_class,
                        )
                case _:
                    visible.update(classes)

        self._registry.set_tools(deferred)
        for tool_name in self._registry.active():
            if (entry := deferred.get(tool_name)) is not None:
                visible[tool_name] = entry[2]
        if not self._registry.is_empty():
            visible[MCP_SEARCH_TOOL_NAME] = self._search_tool_class
        self._visible = list(visible.values())
        self._hidden = hidden

    def _make_change_handler(self, server_name: str) -> Callable[[], None]:
        def handler() -> None:
            self._schedule_refresh(server_name)

        return handler

    def _schedule_refresh(self, server_name: str) -> None:
        task = self._refresh_tasks.get(server_name)
        if task is not None and not task.done():
            return
        self._refresh_tasks[server_name] = asyncio.create_task(
            self._refresh_and_notify(server_name), name=f"mcp-refresh-{server_name}"
        )

    async def _refresh_and_notify(self, server_name: str) -> None:
        try:
            client = self._clients.get(server_name)
            server = self._servers.get(server_name)
            if client is None or server is None:
                return
            tools = await client.list_tools(refresh=True)
            self._server_tools[server_name] = self._build_server_tools(
                client, server, tools
            )
            self._recompute_visible()
            self._notify()
        except Exception as exc:
            logger.warning("MCP tool refresh failed for '%s': %s", server_name, exc)
        finally:
            self._refresh_tasks.pop(server_name, None)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                logger.exception("MCP tools-changed listener failed")
