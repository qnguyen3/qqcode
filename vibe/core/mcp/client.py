from __future__ import annotations

import asyncio
import atexit
from base64 import b64decode
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import mimetypes
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

from vibe.core.config import MCPHttp, MCPServer, MCPStdio, MCPStreamableHttp
from vibe.core.mcp.oauth import (
    MCPAuthError,
    MCPOAuthTokenStore,
    async_refresh_token,
    build_oauth_provider,
    login_hint,
)
from vibe.core.mcp.protocol import (
    MCPToolResult,
    RemoteResource,
    RemoteResourceTemplate,
    RemoteTool,
    ResourceContent,
    parse_call_result,
)

logger = logging.getLogger(__name__)

INITIAL_BACKOFF = 0.5
MAX_BACKOFF = 30.0
MAX_NETWORK_RETRIES = 2
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
AUTH_STATUS = frozenset({401, 403})
AUTH_ERROR_TYPES = (
    MCPAuthError,
    OAuthFlowError,
    OAuthTokenError,
    OAuthRegistrationError,
)

_TEMP_BLOBS: list[Path] = []
_ATEXIT_REGISTERED = False


class MCPServerConnectionError(RuntimeError):
    """Raised when a server cannot be reached or does not finish discovery."""


class MCPServerUnavailableError(RuntimeError):
    """Raised when a call is attempted on a stopped or unreachable client."""


def _cleanup_temp_blobs() -> None:
    while _TEMP_BLOBS:
        path = _TEMP_BLOBS.pop()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _spill_blob(data: bytes, mime_type: str | None) -> Path:
    global _ATEXIT_REGISTERED

    suffix = mimetypes.guess_extension(mime_type or "") or ".bin"
    fd, raw_path = tempfile.mkstemp(prefix="qqcode-mcp-", suffix=suffix)
    path = Path(raw_path)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _TEMP_BLOBS.append(path)
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup_temp_blobs)
        _ATEXIT_REGISTERED = True
    return path


def _iter_exceptions(exc: BaseException) -> Iterator[BaseException]:
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _iter_exceptions(inner)
    if exc.__cause__ is not None:
        yield from _iter_exceptions(exc.__cause__)


def _is_auth_error(exc: BaseException) -> bool:
    for current in _iter_exceptions(exc):
        if isinstance(current, AUTH_ERROR_TYPES):
            return True
        if isinstance(current, httpx.HTTPStatusError) and (
            current.response.status_code in AUTH_STATUS
        ):
            return True
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and status in AUTH_STATUS:
            return True
    return False


def _is_retryable(exc: BaseException) -> bool:
    for current in _iter_exceptions(exc):
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if isinstance(current, httpx.TransportError):
            return True
        if isinstance(current, httpx.HTTPStatusError) and (
            current.response.status_code in RETRYABLE_STATUS
        ):
            return True
        if isinstance(current, OSError):
            return True
    return False


@dataclass
class _ClientRequest:
    method: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None
    future: asyncio.Future[Any] | None = None


class MCPServerClient:
    """Keeps a single ``ClientSession`` open per MCP server.

    The session is owned by a dedicated runner task so the underlying anyio
    cancel scopes are entered and exited in the same task. Requests are
    serialized through a queue. On transport failures the runner retries with
    exponential backoff; calls retry network errors twice and force one
    reconnect when the session is dead.
    """

    def __init__(
        self, config: MCPServer, *, token_store: MCPOAuthTokenStore | None = None
    ) -> None:
        self.config = config
        self.name = config.name
        self._token_store = token_store or MCPOAuthTokenStore()
        self._session: ClientSession | None = None
        self._runner: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._queue: asyncio.Queue[_ClientRequest | None] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._closed = True
        self._last_error: BaseException | None = None
        self._connect_count = 0
        self._tools_cache: list[RemoteTool] | None = None
        self._resources_cache: list[RemoteResource] | None = None
        self._templates_cache: list[RemoteResourceTemplate] | None = None
        self._callbacks: list[Callable[[], None]] = []

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def connect_count(self) -> int:
        return self._connect_count

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    def on_tools_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the server reports a tool change."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        """Connect and initialize. Raises ``MCPServerConnectionError`` on failure."""
        if not self.config.enabled:
            return

        await self._ensure_oauth_token()

        async with self._start_lock:
            runner = self._runner
            if runner is not None and not runner.done() and self._ready.is_set():
                return
            if runner is None or runner.done():
                self._stop_event = asyncio.Event()
                self._queue = asyncio.Queue()
                self._closed = False
                self._runner = asyncio.create_task(
                    self._run(), name=f"mcp-client-{self.name}"
                )
        await self._wait_ready(self.config.timeout)

    async def stop(self) -> None:
        """Close the session and stop the runner task."""
        self._closed = True
        self._stop_event.set()
        runner = self._runner
        self._runner = None
        if runner is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(runner, timeout=5.0)
            except TimeoutError:
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner
            except asyncio.CancelledError:
                runner.cancel()
        self._session = None
        self._ready.clear()
        self._fail_pending(
            MCPServerUnavailableError(f"MCP server '{self.name}' was stopped")
        )

    async def list_tools(self, *, refresh: bool = False) -> list[RemoteTool]:
        if not refresh and self._tools_cache is not None:
            return list(self._tools_cache)
        result = await self._request(
            "list_tools", retries=MAX_NETWORK_RETRIES, timeout=self.config.timeout
        )
        tools = [RemoteTool.model_validate(tool) for tool in result.tools]
        self._tools_cache = tools
        return list(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        result = await self._with_reconnect(
            "call_tool", name, arguments, timeout=self.config.timeout
        )
        return parse_call_result(self.name, name, result)

    async def list_resources(self) -> list[RemoteResource]:
        if self._resources_cache is not None:
            return list(self._resources_cache)
        result = await self._request(
            "list_resources", retries=MAX_NETWORK_RETRIES, timeout=self.config.timeout
        )
        resources = [
            RemoteResource.model_validate(resource) for resource in result.resources
        ]
        self._resources_cache = resources
        return list(resources)

    async def list_resource_templates(self) -> list[RemoteResourceTemplate]:
        if self._templates_cache is not None:
            return list(self._templates_cache)
        result = await self._request(
            "list_resource_templates",
            retries=MAX_NETWORK_RETRIES,
            timeout=self.config.timeout,
        )
        templates = [
            RemoteResourceTemplate.model_validate(template)
            for template in result.resourceTemplates
        ]
        self._templates_cache = templates
        return list(templates)

    async def read_resource(self, uri: str) -> ResourceContent:
        result = await self._with_reconnect(
            "read_resource", AnyUrl(uri), timeout=self.config.timeout
        )
        texts: list[str] = []
        blob_path: str | None = None
        blob_size: int | None = None
        mime_type: str | None = None
        for content in result.contents:
            mime_type = mime_type or getattr(content, "mimeType", None)
            match content:
                case types.TextResourceContents(text=text):
                    texts.append(text)
                case types.BlobResourceContents(blob=blob):
                    data = b64decode(blob)
                    blob_size = (blob_size or 0) + len(data)
                    if blob_path is None:
                        blob_path = str(_spill_blob(data, mime_type))
        return ResourceContent(
            uri=uri,
            mime_type=mime_type,
            text="\n".join(texts) if texts else None,
            blob_size=blob_size,
            blob_path=blob_path,
        )

    def _transport(self) -> AbstractAsyncContextManager[Any]:
        match self.config:
            case MCPStdio():
                argv = self.config.argv()
                if not argv:
                    raise MCPServerConnectionError(
                        f"MCP stdio server '{self.name}' has an empty command"
                    )
                params = StdioServerParameters(
                    command=argv[0], args=argv[1:], env=self.config.stdio_env()
                )
                return stdio_client(params)
            case MCPHttp() | MCPStreamableHttp():
                url = (self.config.url or "").strip()
                if not url:
                    raise MCPServerConnectionError(
                        f"MCP server '{self.name}' is missing a url"
                    )
                headers = self.config.http_headers()
                auth = self._build_auth()
                return streamablehttp_client(url, headers=headers or None, auth=auth)
            case _:
                raise MCPServerConnectionError(
                    f"Unsupported MCP transport: {self.config.transport!r}"
                )

    def _build_auth(self) -> httpx.Auth | None:
        oauth = getattr(self.config, "oauth", None)
        if oauth is None:
            return None
        return build_oauth_provider(self.config, self._token_store, interactive=False)

    async def _ensure_oauth_token(self) -> None:
        """Fail early with a login hint when no usable OAuth token exists."""
        oauth = getattr(self.config, "oauth", None)
        if oauth is None:
            return
        entry = self._token_store.get(self.config) or {}
        raw_tokens = entry.get("tokens")
        if not isinstance(raw_tokens, dict):
            raise MCPAuthError(self._format_auth_error())
        expires_at = entry.get("expires_at")
        if (
            isinstance(expires_at, (int, float))
            and expires_at <= time.time() + 30
            and not await async_refresh_token(self.config, store=self._token_store)
        ):
            raise MCPAuthError(self._format_auth_error())

    async def _run(self) -> None:
        backoff = INITIAL_BACKOFF
        while not self._closed:
            try:
                async with self._transport() as (read, write):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.config.timeout),
                        message_handler=self._on_message,
                    ) as session:
                        await session.initialize()
                        self._session = session
                        self._last_error = None
                        self._connect_count += 1
                        self._ready.set()
                        backoff = INITIAL_BACKOFF
                        await self._serve(session)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._last_error = exc
                if _is_auth_error(exc):
                    logger.debug("MCP server '%s' requires authentication", self.name)
                else:
                    logger.debug(
                        "MCP server '%s' connection failed: %r", self.name, exc
                    )
            finally:
                self._session = None
                self._ready.clear()
                self._invalidate_caches()
            if self._closed:
                break
            try:
                async with asyncio.timeout(backoff):
                    await self._stop_event.wait()
            except TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _serve(self, session: ClientSession) -> None:
        while not self._closed:
            request = await self._queue.get()
            if request is None:
                return
            if request.future is None or request.future.cancelled():
                continue
            try:
                result = await self._dispatch(session, request)
            except asyncio.CancelledError:
                request.future.cancel()
                raise
            except BaseException as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
                return
            if not request.future.done():
                request.future.set_result(result)

    async def _dispatch(self, session: ClientSession, request: _ClientRequest) -> Any:
        method = getattr(session, request.method)
        timeout = (
            request.timeout if request.timeout is not None else self.config.timeout
        )
        async with asyncio.timeout(timeout):
            return await method(*request.args, **request.kwargs)

    async def _request(
        self, method: str, *args: Any, retries: int = 0, timeout: float | None = None
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(retries + 1):
            await self._ensure_ready()
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            await self._queue.put(
                _ClientRequest(method=method, args=args, timeout=timeout, future=future)
            )
            try:
                return await future
            except MCPAuthError:
                raise
            except BaseException as exc:
                if _is_auth_error(exc):
                    raise MCPAuthError(self._format_auth_error()) from exc
                last_error = exc
                if attempt >= retries or not _is_retryable(exc):
                    raise
                await asyncio.sleep(INITIAL_BACKOFF * (2**attempt))
        raise MCPServerUnavailableError(
            f"MCP server '{self.name}' request '{method}' failed: {last_error}"
        )

    async def _with_reconnect(
        self, method: str, *args: Any, timeout: float | None = None
    ) -> Any:
        try:
            return await self._request(
                method, *args, retries=MAX_NETWORK_RETRIES, timeout=timeout
            )
        except MCPAuthError:
            raise
        except BaseException as first_error:
            await self._reconnect()
            try:
                return await self._request(
                    method, *args, retries=MAX_NETWORK_RETRIES, timeout=timeout
                )
            except MCPAuthError:
                raise
            except BaseException as second_error:
                raise MCPServerUnavailableError(
                    f"MCP call to '{self.name}' failed after reconnect: {second_error}"
                ) from first_error

    async def _reconnect(self) -> None:
        runner = self._runner
        self._runner = None
        if runner is not None and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self._closed = False
        self._stop_event = asyncio.Event()
        self._ready.clear()
        await self.start()

    async def _ensure_ready(self) -> None:
        if self._closed and self._runner is None:
            raise MCPServerUnavailableError(f"MCP server '{self.name}' is not started")
        runner = self._runner
        if runner is None or runner.done():
            await self._ensure_oauth_token()
            async with self._start_lock:
                runner = self._runner
                if runner is None or runner.done():
                    self._stop_event = asyncio.Event()
                    self._closed = False
                    self._runner = asyncio.create_task(
                        self._run(), name=f"mcp-client-{self.name}"
                    )
        await self._wait_ready(self.config.timeout)

    async def _wait_ready(self, timeout: float) -> None:
        if self._ready.is_set():
            return
        try:
            async with asyncio.timeout(timeout):
                await self._ready.wait()
        except TimeoutError as exc:
            if self._last_error is not None and _is_auth_error(self._last_error):
                raise MCPAuthError(self._format_auth_error()) from exc
            detail = f": {self._last_error!r}" if self._last_error else ""
            hint = ""
            if getattr(self.config, "oauth", None) is not None:
                hint = (
                    f" If this server requires authentication, "
                    f"{login_hint(self.config)}."
                )
            raise MCPServerConnectionError(
                f"MCP server '{self.name}' did not connect within "
                f"{timeout:.0f}s{detail}.{hint}"
            ) from exc
        if not self._ready.is_set():
            raise MCPServerConnectionError(f"MCP server '{self.name}' is not connected")

    def _fail_pending(self, error: BaseException) -> None:
        while not self._queue.empty():
            request = self._queue.get_nowait()
            if request is None or request.future is None:
                continue
            if not request.future.done():
                request.future.set_exception(error)

    def _invalidate_caches(self) -> None:
        self._tools_cache = None
        self._resources_cache = None
        self._templates_cache = None

    async def _on_message(self, message: Any) -> None:
        if not (
            isinstance(message, types.ServerNotification)
            and isinstance(message.root, types.ToolListChangedNotification)
        ):
            return
        self._invalidate_caches()
        for callback in list(self._callbacks):
            try:
                callback()
            except Exception:
                logger.exception("MCP tools-changed callback failed for %s", self.name)

    def _format_auth_error(self) -> str:
        return (
            f"MCP server '{self.name}' requires authentication. "
            f"{login_hint(self.config)}"
        )


def server_display_target(server: MCPServer) -> str:
    match server:
        case MCPStdio():
            return " ".join(server.argv())
        case MCPHttp() | MCPStreamableHttp():
            return server.url
        case _:
            return server.name
