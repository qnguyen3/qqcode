"""Example 04: Run multiple independent AgentSession instances concurrently."""

from __future__ import annotations

import asyncio

from vibe.sdk import AgentOptions, MessageDeltaEvent, create_agent


async def run_worker(name: str, question: str) -> str:
    chunks: list[str] = []
    async with create_agent(AgentOptions(auto_approve=True, save_session=False)) as s:
        async for event in s.prompt(question):
            if isinstance(event, MessageDeltaEvent):
                chunks.append(event.delta)
    return f"{name} ({s.session_id[:8]}): {''.join(chunks)}"


async def main() -> None:
    results = await asyncio.gather(
        run_worker("worker-a", "List main packages in vibe/"),
        run_worker("worker-b", "Describe the session log format."),
    )
    for line in results:
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
