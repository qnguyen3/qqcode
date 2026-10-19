from __future__ import annotations

import asyncio
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from vibe.core.config import MCPHttp, MCPOAuthConfig, MCPServer, MCPStreamableHttp

TOKEN_STORE_PATH = Path.home() / ".qqcode" / "mcp-auth.json"

DEFAULT_LOGIN_TIMEOUT = 300.0

HTTP_OK = 200


class MCPAuthError(RuntimeError):
    """Raised when an MCP OAuth flow or token refresh fails."""


class MCPOAuthRequiredError(MCPAuthError):
    """Raised when a protected server needs an interactive ``mcp login``."""


def server_key(server: MCPServer) -> str:
    match server:
        case MCPHttp() | MCPStreamableHttp():
            return f"{server.name}|{server.url}"
        case _:
            return server.name


def login_hint(server: MCPServer) -> str:
    return f"run `qqcode mcp login {server.name}`"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class MCPOAuthTokenStore:
    """JSON token store keyed by server name and URL.

    The file lives at ``~/.qqcode/mcp-auth.json`` and is written atomically
    with owner-only permissions.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or TOKEN_STORE_PATH
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, server: MCPServer) -> dict[str, Any] | None:
        with self._lock:
            entry = self._read().get(server_key(server))
        return entry if isinstance(entry, dict) else None

    def set(
        self,
        server: MCPServer,
        *,
        tokens: dict[str, Any] | None = None,
        client_info: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            data = self._read()
            entry = data.get(server_key(server)) or {}
            if tokens is not None:
                entry["tokens"] = tokens
            if client_info is not None:
                entry["client_info"] = client_info
            entry["server"] = server.name
            entry["url"] = str(getattr(server, "url", ""))
            data[server_key(server)] = entry
            _atomic_write_json(self.path, data)

    def delete(self, server: MCPServer) -> bool:
        with self._lock:
            data = self._read()
            if server_key(server) not in data:
                return False
            del data[server_key(server)]
            _atomic_write_json(self.path, data)
            return True

    def has_tokens(self, server: MCPServer) -> bool:
        entry = self.get(server) or {}
        return isinstance(entry.get("tokens"), dict)


def _tokens_payload(tokens: OAuthToken) -> dict[str, Any]:
    payload = tokens.model_dump(mode="json", exclude_none=True)
    if tokens.expires_in is not None:
        payload["expires_at"] = time.time() + tokens.expires_in
    return payload


class _SDKTokenStorage(TokenStorage):
    def __init__(self, store: MCPOAuthTokenStore, server: MCPServer) -> None:
        self._store = store
        self._server = server

    async def get_tokens(self) -> OAuthToken | None:
        entry = self._store.get(self._server) or {}
        raw = entry.get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValueError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._store.set(self._server, tokens=_tokens_payload(tokens))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        entry = self._store.get(self._server) or {}
        raw = entry.get("client_info")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._store.set(
            self._server,
            client_info=client_info.model_dump(mode="json", exclude_none=True),
        )


class OAuthCallbackServer:
    """Loopback HTTP server capturing the authorization code of an OAuth flow."""

    def __init__(self, port: int | None = None) -> None:
        self._results: queue.Queue[tuple[str | None, str | None]] = queue.Queue()

        results = self._results

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                params = parse_qs(parsed.query)
                code = (params.get("code") or [None])[0]
                state = (params.get("state") or [None])[0]
                error = (params.get("error") or [None])[0]
                results.put((code if not error else None, state))
                body = (
                    b"<html><body><h3>QQcode MCP authentication complete.</h3>"
                    b"<p>You can close this window.</p></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._httpd = HTTPServer(("127.0.0.1", port or 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="mcp-oauth-callback"
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    async def wait(self, timeout: float) -> tuple[str, str | None]:
        try:
            code, state = await asyncio.to_thread(self._results.get, True, timeout)
        except queue.Empty as exc:
            raise MCPAuthError("Timed out waiting for the OAuth callback") from exc
        if not code:
            raise MCPAuthError("OAuth callback did not include an authorization code")
        return code, state

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _oauth_config(server: MCPServer) -> MCPOAuthConfig:
    oauth = getattr(server, "oauth", None)
    if oauth is None:
        raise MCPAuthError(f"MCP server '{server.name}' has no oauth configuration")
    return oauth


def _client_info(
    oauth: MCPOAuthConfig, redirect_uri: str
) -> OAuthClientInformationFull:
    secret = oauth.client_secret
    return OAuthClientInformationFull(
        client_id=oauth.client_id,
        client_secret=secret,
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="client_secret_basic" if secret else "none",
    )


def build_oauth_provider(
    server: MCPServer,
    store: MCPOAuthTokenStore,
    *,
    interactive: bool = False,
    callback_server: OAuthCallbackServer | None = None,
    open_browser: bool = True,
    printer: Callable[[str], None] = print,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
) -> OAuthClientProvider:
    """Build an SDK ``OAuthClientProvider`` for an HTTP MCP server.

    In interactive mode the provider opens the authorization URL and waits on
    ``callback_server`` for the redirect; otherwise a missing token raises
    ``MCPOAuthRequiredError`` so callers can point at ``qqcode mcp login``.
    """
    oauth = _oauth_config(server)
    url = str(getattr(server, "url", ""))
    redirect_uri = (
        callback_server.redirect_uri
        if callback_server is not None
        else f"http://127.0.0.1:{oauth.callback_port or 0}/callback"
    )

    metadata = OAuthClientMetadata(
        client_name="qqcode",
        redirect_uris=[AnyUrl(redirect_uri)],
        scope=" ".join(oauth.scopes) if oauth.scopes else None,
        token_endpoint_auth_method="client_secret_basic"
        if oauth.client_secret
        else None,
    )

    if interactive and callback_server is not None:

        async def redirect_handler(authorization_url: str) -> None:
            printer(f"Open this URL to authenticate:\n{authorization_url}")
            if open_browser:
                webbrowser.open(authorization_url)

        async def callback_handler() -> tuple[str, str | None]:
            return await callback_server.wait(timeout)

    else:

        async def redirect_handler(authorization_url: str) -> None:
            raise MCPOAuthRequiredError(
                f"MCP server '{server.name}' requires authentication: {login_hint(server)}"
            )

        async def callback_handler() -> tuple[str, str | None]:
            raise MCPOAuthRequiredError(
                f"MCP server '{server.name}' requires authentication: {login_hint(server)}"
            )

    provider = OAuthClientProvider(
        url,
        metadata,
        _SDKTokenStorage(store, server),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=timeout,
    )
    if oauth.auth_server_url:
        provider.context.auth_server_url = oauth.auth_server_url
    return provider


async def async_login(
    server: MCPServer,
    *,
    open_browser: bool = True,
    printer: Callable[[str], None] = print,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    store: MCPOAuthTokenStore | None = None,
) -> bool:
    """Run the OAuth authorization-code flow and persist the resulting token.

    Raises:
        MCPAuthError: If the server has no OAuth config or the flow fails.
    """
    oauth = _oauth_config(server)
    token_store = store or MCPOAuthTokenStore()
    try:
        callback_server = OAuthCallbackServer(oauth.callback_port)
    except OSError as exc:
        raise MCPAuthError(
            f"Cannot bind the OAuth callback port {oauth.callback_port}: {exc}"
        ) from exc
    try:
        if oauth.client_id:
            info = _client_info(oauth, callback_server.redirect_uri)
            token_store.set(
                server, client_info=info.model_dump(mode="json", exclude_none=True)
            )

        provider = build_oauth_provider(
            server,
            token_store,
            interactive=True,
            callback_server=callback_server,
            open_browser=open_browser,
            printer=printer,
            timeout=timeout,
        )
        async with httpx.AsyncClient(auth=provider, timeout=30.0) as client:
            await client.get(
                str(getattr(server, "url", "")),
                headers={"Accept": "application/json, text/event-stream"},
            )
    except MCPOAuthRequiredError:
        raise
    except Exception as exc:
        raise MCPAuthError(f"OAuth login for '{server.name}' failed: {exc}") from exc
    finally:
        callback_server.close()

    if not token_store.has_tokens(server):
        raise MCPAuthError(
            f"OAuth login for '{server.name}' finished without obtaining a token"
        )
    return True


async def _load_auth_metadata(server: MCPServer) -> OAuthMetadata:
    oauth = _oauth_config(server)
    if oauth.auth_server_url:
        base = oauth.auth_server_url.rstrip("/")
    else:
        parsed = urlparse(str(getattr(server, "url", "")))
        base = f"{parsed.scheme}://{parsed.netloc}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{base}/.well-known/oauth-authorization-server")
    if response.status_code != HTTP_OK:
        raise MCPAuthError(
            f"OAuth metadata discovery failed ({response.status_code}) for '{server.name}'"
        )
    return OAuthMetadata.model_validate(response.json())


async def async_refresh_token(
    server: MCPServer, *, store: MCPOAuthTokenStore | None = None
) -> bool:
    """Refresh the stored access token. Returns False when refresh is impossible."""
    token_store = store or MCPOAuthTokenStore()
    entry = token_store.get(server) or {}
    raw_tokens = entry.get("tokens")
    raw_client = entry.get("client_info")
    try:
        tokens = OAuthToken.model_validate(raw_tokens)
        client_info = (
            OAuthClientInformationFull.model_validate(raw_client)
            if isinstance(raw_client, dict)
            else None
        )
    except (TypeError, ValueError):
        return False
    if not tokens.refresh_token:
        return False

    try:
        metadata = await _load_auth_metadata(server)
        token_url = str(metadata.token_endpoint) if metadata.token_endpoint else ""
    except MCPAuthError:
        return False
    if not token_url:
        return False

    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
    }
    auth: tuple[str, str] | None = None
    if client_info and client_info.client_id:
        data["client_id"] = client_info.client_id
        if client_info.client_secret:
            auth = (client_info.client_id, client_info.client_secret)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            token_url,
            data=data,
            auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code != HTTP_OK:
        return False

    new_tokens = OAuthToken.model_validate(response.json())
    if new_tokens.refresh_token is None:
        new_tokens.refresh_token = tokens.refresh_token
    token_store.set(server, tokens=_tokens_payload(new_tokens))
    return True


def logout(server: MCPServer, *, store: MCPOAuthTokenStore | None = None) -> bool:
    return (store or MCPOAuthTokenStore()).delete(server)
