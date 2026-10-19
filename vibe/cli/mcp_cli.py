from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tomllib
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from rich import print as rprint
from rich.table import Table
import tomli_w

from vibe.core.config import (
    CONFIG_FILE,
    MCPServer,
    MCPStdio,
    VibeConfig,
    load_api_keys_from_env,
)
from vibe.core.mcp.client import MCPServerClient
from vibe.core.mcp.manager import MCPManager, MCPServerStatus
from vibe.core.mcp.oauth import MCPAuthError, async_login, logout

_SERVER_ADAPTER: TypeAdapter[MCPServer] = TypeAdapter(MCPServer)

USAGE = (
    "usage: qqcode mcp [list [--json] | add <name> --url URL [--oauth] "
    "[--header K=V] [--timeout S] | add <name> [--env K=V] -- cmd args... | "
    "remove <name> [--local] | login <name> [--no-browser] | logout <name>]"
)


class _AddError(ValueError):
    """Raised when ``qqcode mcp add`` arguments are inconsistent."""


def _parse_kv(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values or []:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            raise _AddError(f"Expected KEY=VALUE, got '{raw}'")
        result[key] = value
    return result


def _load_config() -> VibeConfig:
    load_api_keys_from_env()
    return VibeConfig.load()


def _find_server(config: VibeConfig, name: str) -> MCPServer:
    for server in config.mcp_servers:
        if server.name == name:
            return server
    raise KeyError(name)


async def _probe_server(server: MCPServer, *, timeout: float | None) -> list[str]:
    if timeout is not None:
        server.timeout = timeout
    client = MCPServerClient(server)
    try:
        await client.start()
        tools = await client.list_tools(refresh=True)
        return [tool.name for tool in tools]
    finally:
        await client.stop()


def _local_config_file() -> Path:
    return Path.cwd() / ".qqcode" / "config.toml"


def _read_raw_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def _servers_as_dicts(config: VibeConfig) -> list[dict[str, Any]]:
    return [
        server.model_dump(mode="json", exclude_none=True)
        for server in config.mcp_servers
    ]


def _write_servers(servers: list[dict[str, Any]], *, local: bool) -> None:
    if not local:
        VibeConfig.save_updates({"mcp_servers": servers})
        return
    path = _local_config_file()
    data = _read_raw_config(path)
    data["mcp_servers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        tomli_w.dump(to_jsonable_python(data, exclude_none=True), handle)


def run_mcp_command(argv: list[str]) -> int:
    """Entry point for ``qqcode mcp <subcommand>``."""
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0 if argv else 1

    handlers: dict[str, Any] = {
        "list": _cmd_list,
        "add": _cmd_add,
        "remove": _cmd_remove,
        "login": _cmd_login,
        "logout": _cmd_logout,
    }
    handler = handlers.get(argv[0])
    if handler is None:
        print(f"Unknown mcp subcommand: {argv[0]}\n{USAGE}", file=sys.stderr)
        return 1
    return handler(argv[1:])


def _cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qqcode mcp list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config()
    statuses = asyncio.run(_collect_statuses(config))

    if args.json:
        print(json.dumps([status.model_dump() for status in statuses]))
        return 0
    _render_status_table(statuses)
    return 0


async def _collect_statuses(config: VibeConfig) -> list[MCPServerStatus]:
    manager = MCPManager(config)
    try:
        await manager.start()
        return await manager.server_statuses()
    finally:
        await manager.stop()


def _render_status_table(statuses: list[MCPServerStatus]) -> None:
    if not statuses:
        print("No MCP servers configured.")
        return

    table = Table(title="MCP servers")
    table.add_column("name")
    table.add_column("transport")
    table.add_column("target")
    table.add_column("status")
    table.add_column("tools")
    for status in statuses:
        match status:
            case MCPServerStatus(enabled=False):
                state = "disabled"
            case MCPServerStatus(connected=True):
                state = "connected"
            case _:
                state = "unreachable"
        tools = "-" if status.tools is None else str(status.tools)
        table.add_row(status.name, status.transport, status.target, state, tools)
    rprint(table)
    for status in statuses:
        if status.error:
            rprint(f"[yellow]{status.name}: {status.error}[/]")


def _split_stdio(argv: list[str]) -> tuple[list[str], list[str] | None]:
    if "--" not in argv:
        return argv, None
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def _add_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qqcode mcp add")
    parser.add_argument("name")
    parser.add_argument("--url")
    parser.add_argument(
        "--transport", choices=["http", "streamable-http"], default="streamable-http"
    )
    parser.add_argument("--oauth", action="store_true")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--local", action="store_true")
    return parser


def _add_server_dict(
    args: argparse.Namespace, stdio_command: list[str] | None
) -> dict[str, Any]:
    if stdio_command is not None:
        if args.url:
            raise _AddError("--url cannot be combined with a stdio command")
        if not stdio_command:
            raise _AddError("Missing command after '--'")
        if args.header:
            raise _AddError("--header is only supported for HTTP servers")
        server_dict: dict[str, Any] = {
            "name": args.name,
            "transport": "stdio",
            "command": stdio_command[0],
            "args": stdio_command[1:],
        }
        if env := _parse_kv(args.env):
            server_dict["env"] = env
    else:
        if not args.url:
            raise _AddError("Either --url URL or '-- cmd args...' is required")
        if args.env:
            raise _AddError("--env is only supported for stdio servers")
        server_dict = {"name": args.name, "transport": args.transport, "url": args.url}
        if headers := _parse_kv(args.header):
            server_dict["headers"] = headers
        if args.oauth:
            server_dict["oauth"] = {}
    if args.timeout is not None:
        server_dict["timeout"] = args.timeout
    return server_dict


def _verify_server(server: MCPServer, name: str, timeout: float | None) -> str | None:
    """Return an error message when the connection probe fails."""
    try:
        tools = asyncio.run(_probe_server(server, timeout=timeout))
    except MCPAuthError as exc:
        return str(exc)
    except Exception as exc:
        return f"Could not connect to MCP server '{name}': {exc}"
    print(f"Connected to '{name}': {len(tools)} tool(s)")
    return None


def _cmd_add(argv: list[str]) -> int:
    head, stdio_command = _split_stdio(argv)
    args = _add_parser().parse_args(head)

    try:
        server_dict = _add_server_dict(args, stdio_command)
        server = _SERVER_ADAPTER.validate_python(server_dict)
    except (_AddError, ValidationError) as exc:
        print(f"Invalid MCP server configuration: {exc}", file=sys.stderr)
        return 1

    if not args.no_verify and (
        error := _verify_server(server, args.name, args.timeout)
    ):
        print(error, file=sys.stderr)
        return 1

    config = _load_config()
    servers = [
        existing
        for existing in _servers_as_dicts(config)
        if existing.get("name") != server.name
    ]
    servers.append(server.model_dump(mode="json", exclude_none=True))
    try:
        _write_servers(servers, local=args.local)
    except OSError as exc:
        print(f"Failed to write config: {exc}", file=sys.stderr)
        return 1
    target = _local_config_file() if args.local else CONFIG_FILE
    print(f"Added MCP server '{server.name}' to {target}")
    return 0


def _cmd_remove(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qqcode mcp remove")
    parser.add_argument("name")
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config()
    remaining = [
        server
        for server in _servers_as_dicts(config)
        if server.get("name") != args.name
    ]
    if len(remaining) == len(config.mcp_servers):
        print(f"MCP server '{args.name}' not found", file=sys.stderr)
        return 1

    try:
        _write_servers(remaining, local=args.local)
    except OSError as exc:
        print(f"Failed to write config: {exc}", file=sys.stderr)
        return 1
    target = _local_config_file() if args.local else CONFIG_FILE
    print(f"Removed MCP server '{args.name}' from {target}")
    return 0


def _cmd_login(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qqcode mcp login")
    parser.add_argument("name")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config()
    try:
        server = _find_server(config, args.name)
    except KeyError:
        print(f"MCP server '{args.name}' not found", file=sys.stderr)
        return 1

    if isinstance(server, MCPStdio):
        print(
            f"MCP server '{args.name}' uses stdio and needs no login", file=sys.stderr
        )
        return 1

    if getattr(server, "oauth", None) is None:
        print(
            f"MCP server '{args.name}' has no oauth configuration; "
            f"re-add it with --oauth",
            file=sys.stderr,
        )
        return 1

    try:
        asyncio.run(async_login(server, open_browser=not args.no_browser))
    except MCPAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Authenticated to MCP server '{args.name}'")
    return 0


def _cmd_logout(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qqcode mcp logout")
    parser.add_argument("name")
    args = parser.parse_args(argv)

    config = _load_config()
    try:
        server = _find_server(config, args.name)
    except KeyError:
        print(f"MCP server '{args.name}' not found", file=sys.stderr)
        return 1

    if logout(server):
        print(f"Removed stored credentials for MCP server '{args.name}'")
        return 0
    print(f"No stored credentials for MCP server '{args.name}'", file=sys.stderr)
    return 1
