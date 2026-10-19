from __future__ import annotations

from typing import TYPE_CHECKING

from vibe.core.sessions.entries import SessionEntry

if TYPE_CHECKING:
    from vibe.core.agent import Agent
    from vibe.core.interaction_logger import InteractionLogger


class SessionRecorder:
    """Thin bridge between the agent lifecycle and the session store.

    The agent owns the entry log and assigns entry ids; the recorder only
    persists those entries and saves the legacy ``.json`` snapshot.
    """

    def __init__(self, logger: InteractionLogger, agent: Agent | None = None) -> None:
        self._logger = logger
        self._agent = agent

    @property
    def logger(self) -> InteractionLogger:
        return self._logger

    def write(self, entry: SessionEntry) -> None:
        self._logger.append_entry(entry)

    async def flush(self) -> None:
        agent = self._agent
        if agent is None:
            return
        await self._logger.save_interaction(
            agent.messages,
            agent.stats,
            agent.config,
            agent.tool_manager,
            agent._additional_directory_contexts,
        )
