from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.mcp.helpers import stdio_server
from vibe.core.mcp.client import (
    MCPServerClient,
    MCPServerConnectionError,
    MCPServerUnavailableError,
)
from vibe.core.mcp.oauth import MCPAuthError


@pytest.mark.asyncio
async def test_two_calls_reuse_one_connection(tmp_path: Path) -> None:
    state = tmp_path / "state.log"
    client = MCPServerClient(stdio_server(state_file=state))
    await client.start()
    try:
        first = await client.call_tool("echo", {"text": "one"})
        second = await client.call_tool("echo", {"text": "two"})

        assert first.ok and second.ok
        assert client.connect_count == 1
        starts = state.read_text().splitlines().count("start")
        assert starts == 1
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_list_tools_is_cached_until_refreshed(tmp_path: Path) -> None:
    state = tmp_path / "state.log"
    client = MCPServerClient(stdio_server(state_file=state))
    await client.start()
    try:
        first = await client.list_tools()
        second = await client.list_tools()
        assert first == second

        await client.call_tool("trigger_change", {})

        refreshed = first
        async with asyncio.timeout(10):
            while "late_tool" not in {tool.name for tool in refreshed}:
                await asyncio.sleep(0.05)
                refreshed = await client.list_tools()
        assert "late_tool" in {tool.name for tool in refreshed}
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_reconnect_after_server_crash(tmp_path: Path) -> None:
    state = tmp_path / "state.log"
    client = MCPServerClient(stdio_server(state_file=state))
    await client.start()
    try:
        with pytest.raises((MCPServerUnavailableError, MCPServerConnectionError)):
            await client.call_tool("crash", {})

        async with asyncio.timeout(15):
            while True:
                try:
                    result = await client.call_tool("echo", {"text": "back"})
                    break
                except Exception:
                    await asyncio.sleep(0.2)

        assert result.structured == {"result": "echo:back"}
        starts = state.read_text().splitlines().count("start")
        assert starts >= 2
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stop_closes_session(tmp_path: Path) -> None:
    client = MCPServerClient(stdio_server(state_file=tmp_path / "state.log"))
    await client.start()
    assert client.connected

    await client.stop()

    assert not client.connected
    with pytest.raises(MCPServerUnavailableError):
        await client.list_tools(refresh=True)


@pytest.mark.asyncio
async def test_structured_and_text_results(tmp_path: Path) -> None:
    client = MCPServerClient(stdio_server(state_file=tmp_path / "state.log"))
    await client.start()
    try:
        structured = await client.call_tool("add", {"a": 2, "b": 40})
        assert structured.structured == {"sum": 42}
        assert structured.text is None
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_missing_oauth_token_requires_login() -> None:
    from tests.mcp.fake_oauth_server import FakeOAuthServer
    from vibe.core.config import MCPOAuthConfig, MCPStreamableHttp
    from vibe.core.mcp.oauth import MCPOAuthTokenStore

    fake = FakeOAuthServer()
    server = MCPStreamableHttp(
        name="protected",
        transport="streamable-http",
        url=fake.mcp_url,
        oauth=MCPOAuthConfig(),
        timeout=2.0,
    )
    client = MCPServerClient(
        server, token_store=MCPOAuthTokenStore(Path("/nonexistent/mcp-auth.json"))
    )
    try:
        with pytest.raises(MCPAuthError, match="qqcode mcp login protected"):
            await client.start()
    finally:
        await client.stop()
        fake.close()
