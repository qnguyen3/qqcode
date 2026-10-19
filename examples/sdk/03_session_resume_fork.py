"""Example 03: Resume and fork persisted sessions via SessionRef."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

from vibe.sdk import AgentOptions, SessionRef, create_agent


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        async with create_agent(
            AgentOptions(auto_approve=True, session_dir=session_dir)
        ) as first:
            async for _ in first.prompt("Remember the code word: ORBIT."):
                pass
            saved_id = first.session_id

        async with create_agent(
            AgentOptions(
                auto_approve=True,
                session_dir=session_dir,
                session=SessionRef(session_id=saved_id),
            )
        ) as resumed:
            print(f"Resumed session {resumed.session_id} with {len(resumed.messages())} messages")


if __name__ == "__main__":
    asyncio.run(main())
