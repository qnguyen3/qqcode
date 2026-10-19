from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import threading
import webbrowser

import httpx
import pytest

from tests.mcp.fake_oauth_server import FakeOAuthServer
from vibe.core.config import MCPOAuthConfig, MCPStreamableHttp
from vibe.core.mcp.oauth import (
    MCPOAuthTokenStore,
    async_login,
    async_refresh_token,
    logout,
)


@pytest.fixture
def fake_oauth() -> Iterator[FakeOAuthServer]:
    fake = FakeOAuthServer()
    yield fake
    fake.close()


@pytest.fixture
def store(tmp_path: Path) -> MCPOAuthTokenStore:
    return MCPOAuthTokenStore(tmp_path / "mcp-auth.json")


def _server(
    fake: FakeOAuthServer, name: str = "protected", url: str | None = None
) -> MCPStreamableHttp:
    return MCPStreamableHttp(
        name=name,
        transport="streamable-http",
        url=url or fake.mcp_url,
        oauth=MCPOAuthConfig(),
        timeout=5.0,
    )


def _auto_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(url: str) -> bool:
        def _follow() -> None:
            httpx.get(url, timeout=10, follow_redirects=True)

        threading.Thread(target=_follow, daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)


def test_token_store_is_owner_only_and_keyed_by_name_url(
    store: MCPOAuthTokenStore,
) -> None:
    first = MCPStreamableHttp(
        name="alpha",
        transport="streamable-http",
        url="http://one.invalid/mcp",
        oauth=MCPOAuthConfig(),
    )
    second = MCPStreamableHttp(
        name="alpha",
        transport="streamable-http",
        url="http://two.invalid/mcp",
        oauth=MCPOAuthConfig(),
    )

    store.set(first, tokens={"access_token": "one"})
    store.set(second, tokens={"access_token": "two"})

    raw = json.loads(store.path.read_text())
    assert set(raw) == {"alpha|http://one.invalid/mcp", "alpha|http://two.invalid/mcp"}
    assert store.has_tokens(first)
    mode = store.path.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.asyncio
async def test_login_completes_pkce_exchange_and_logout(
    fake_oauth: FakeOAuthServer,
    store: MCPOAuthTokenStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _auto_browser(monkeypatch)
    server = _server(fake_oauth)

    assert await async_login(server, store=store, printer=lambda _: None, timeout=15)

    assert store.has_tokens(server)
    assert fake_oauth.pkce_verified is True
    assert fake_oauth.token_exchange_count == 1
    entry = store.get(server) or {}
    assert entry["tokens"]["access_token"].startswith("access-")
    assert "expires_at" in entry["tokens"]
    assert logout(server, store=store) is True
    assert not store.has_tokens(server)


@pytest.mark.asyncio
async def test_refresh_rotates_token(
    fake_oauth: FakeOAuthServer,
    store: MCPOAuthTokenStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _auto_browser(monkeypatch)
    server = _server(fake_oauth)
    assert await async_login(server, store=store, printer=lambda _: None, timeout=15)

    assert await async_refresh_token(server, store=store) is True

    assert fake_oauth.refresh_count == 1
    entry = store.get(server) or {}
    assert entry["tokens"]["access_token"] == "access-refresh-1"
    assert entry["tokens"]["refresh_token"] == "refresh-refresh-1"


@pytest.mark.asyncio
async def test_refresh_without_token_fails(
    fake_oauth: FakeOAuthServer, store: MCPOAuthTokenStore
) -> None:
    server = _server(fake_oauth)

    assert await async_refresh_token(server, store=store) is False
