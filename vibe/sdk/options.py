"""Configuration and session reference models for the Embedding SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibe.core.config import VibeConfig

type ExecutionModeName = Literal["plan", "interactive", "auto-approve"]


class SessionRef(BaseModel):
    """Reference to an existing session to resume or fork."""

    session_id: str | None = None
    fork_at_entry_id: str | None = None


class AgentOptions(BaseModel):
    """Options for constructing an embedded :class:`~vibe.sdk.agent.AgentSession`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VibeConfig | None = None
    config_path: Path | None = None
    agent: str | None = None
    model_alias: str | None = None
    enabled_tools: list[str] | None = None
    system_prompt: str | None = None
    append_system_prompt: list[str] = Field(default_factory=list)
    auto_approve: bool = False
    mode: ExecutionModeName | None = None
    working_dir: Path = Field(default_factory=Path.cwd)
    save_session: bool = True
    session_dir: Path | None = None
    session: SessionRef | None = None
    max_turns: int | None = None
    max_price: float | None = None
    enable_streaming: bool = False
    backend: Any = Field(default=None, exclude=True)
