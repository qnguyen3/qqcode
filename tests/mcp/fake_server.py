from __future__ import annotations

import os
from pathlib import Path
import sys

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("qqcode-fake-mcp")

STATE_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else None
LATE_TOOL_ADDED = False


def _record(event: str) -> None:
    if STATE_FILE is not None:
        with STATE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{event}\n")


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back to the caller."""
    _record("tool:echo")
    return f"echo:{text}"


@mcp.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers and return structured output."""
    _record("tool:add")
    return {"sum": a + b}


def _late_tool() -> str:
    """A tool added after startup."""
    _record("tool:late")
    return "late"


@mcp.tool()
async def trigger_change(ctx: Context) -> str:
    """Add a new tool and notify the client that the list changed."""
    global LATE_TOOL_ADDED
    if not LATE_TOOL_ADDED:
        mcp.add_tool(_late_tool, name="late_tool")
        LATE_TOOL_ADDED = True
    await ctx.session.send_tool_list_changed()
    _record("tool:trigger_change")
    return "changed"


@mcp.tool()
def crash() -> str:
    """Kill the server process to simulate a dropped connection."""
    _record("tool:crash")
    os._exit(3)
    return "unreachable"


@mcp.resource("resource://greeting")
def greeting() -> str:
    """A plain text resource."""
    _record("resource:greeting")
    return "hello from fake mcp"


@mcp.resource("resource://blob")
def blob() -> bytes:
    """A binary resource."""
    _record("resource:blob")
    return b"\x00\x01\x02\xffbinary"


@mcp.resource("resource://big")
def big() -> str:
    """A deliberately large text resource."""
    _record("resource:big")
    return "\n".join(f"line {index}" for index in range(5000))


@mcp.resource("resource://items/{item_id}")
def item(item_id: str) -> str:
    """A templated resource."""
    _record(f"resource:item:{item_id}")
    return f"item {item_id}"


def main() -> None:
    _record("start")
    mcp.run()


if __name__ == "__main__":
    main()
