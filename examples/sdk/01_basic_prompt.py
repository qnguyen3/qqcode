"""Example 01: Stream a single prompt through the QQcode Embedding SDK."""

from __future__ import annotations

import asyncio

from vibe.sdk import AgentOptions, MessageDeltaEvent, TurnCompletedEvent, create_agent


async def main() -> None:
    options = AgentOptions(auto_approve=True, save_session=False)
    async with create_agent(options) as session:
        async for event in session.prompt("Summarize the purpose of this repository."):
            match event:
                case MessageDeltaEvent(delta=delta):
                    print(delta, end="", flush=True)
                case TurnCompletedEvent(usage=usage, cost_usd=cost):
                    print(
                        f"\n[completed: in={usage.input} out={usage.output} cost=${cost:.6f}]"
                    )


if __name__ == "__main__":
    asyncio.run(main())
