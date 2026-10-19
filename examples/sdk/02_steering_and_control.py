"""Example 02: Runtime controls (steer, set_model, set_thinking, compact, abort)."""

from __future__ import annotations

import asyncio

from vibe.sdk import AgentOptions, create_agent


async def main() -> None:
    async with create_agent(AgentOptions(auto_approve=True, save_session=False)) as s:
        await s.set_thinking("low")
        await s.steer("Keep the answer under three bullet points.")
        async for event in s.prompt("Explain how context compaction works."):
            print(event.type)
        await s.compact(instructions="Preserve key architectural points.")


if __name__ == "__main__":
    asyncio.run(main())
