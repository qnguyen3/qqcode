"""Test that plan mode instructions are injected into system prompt, not user messages."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent import Agent
from vibe.core.config import VibeConfig
from vibe.core.prompts import UtilityPrompt
from vibe.core.types import AgentMode, LLMChunk, LLMMessage, LLMUsage, Role


@pytest.fixture
def backend() -> FakeBackend:
    """Fake backend that returns a simple response."""
    return FakeBackend(
        results=[
            LLMChunk(
                message=LLMMessage(role=Role.assistant, content="Hi"),
                finish_reason="end_turn",
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
            )
        ]
    )


@pytest.fixture
def config() -> VibeConfig:
    """Basic config for testing."""
    return VibeConfig(workdir=Path.cwd())


class TestPlanModeSystemPrompt:
    """Test that plan mode instructions are in system prompt."""

    def test_initial_system_prompt_without_plan_mode(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """System prompt should NOT contain plan mode instructions initially."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.INTERACTIVE)

        system_prompt = agent.messages[0].content
        plan_mode_marker = "PLAN MODE"

        assert plan_mode_marker not in system_prompt

    def test_initial_system_prompt_with_plan_mode(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """System prompt SHOULD contain plan mode instructions when initialized in plan mode."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.PLAN)

        system_prompt = agent.messages[0].content
        plan_mode_marker = "PLAN MODE"

        assert plan_mode_marker in system_prompt

    def test_system_prompt_regenerated_when_switching_to_plan_mode(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """System prompt should be regenerated when switching to plan mode."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.INTERACTIVE)

        # Initial system prompt should not have plan mode
        initial_prompt = agent.messages[0].content
        assert "PLAN MODE" not in initial_prompt

        # Switch to plan mode
        agent.mode = AgentMode.PLAN

        # System prompt should now include plan mode instructions
        updated_prompt = agent.messages[0].content
        assert "PLAN MODE" in updated_prompt
        assert updated_prompt != initial_prompt

    def test_system_prompt_regenerated_when_exiting_plan_mode(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """System prompt should be regenerated when exiting plan mode."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.PLAN)

        # Initial system prompt should have plan mode
        initial_prompt = agent.messages[0].content
        assert "PLAN MODE" in initial_prompt

        # Exit plan mode
        agent.mode = AgentMode.INTERACTIVE

        # System prompt should no longer include plan mode instructions
        updated_prompt = agent.messages[0].content
        assert "PLAN MODE" not in updated_prompt
        assert updated_prompt != initial_prompt

    def test_system_prompt_not_regenerated_if_mode_unchanged(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """System prompt should NOT be regenerated if mode is set to the same value."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.INTERACTIVE)

        initial_prompt = agent.messages[0].content
        initial_message_obj = agent.messages[0]

        # Set mode to the same value
        agent.mode = AgentMode.INTERACTIVE

        # System prompt should be unchanged (same object)
        assert agent.messages[0] is initial_message_obj
        assert agent.messages[0].content == initial_prompt

    @pytest.mark.asyncio
    async def test_user_messages_do_not_contain_plan_mode_prefix(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """User messages should NOT contain plan mode instructions prefix."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.PLAN)
        agent.message_observer = MagicMock()

        # Send a user message
        user_message_content = "Create a new feature"
        async for _event in agent.act(user_message_content):
            pass

        # Find the user message in the conversation
        user_messages = [msg for msg in agent.messages if msg.role == Role.user]
        assert len(user_messages) == 1

        user_msg = user_messages[0]

        # User message should be exactly what was sent, no prefix
        assert user_msg.content == user_message_content
        assert not user_msg.content.startswith("# Plan Mode")
        assert "PLAN MODE" not in user_msg.content

    def test_plan_mode_prompt_content_is_included(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """Verify the actual plan mode prompt content is in system prompt."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.PLAN)

        system_prompt = agent.messages[0].content
        plan_mode_instructions = UtilityPrompt.PLAN_MODE.read()

        # The plan mode instructions should be in the system prompt
        assert plan_mode_instructions in system_prompt

    def test_mode_cycling_updates_system_prompt(
        self, backend: FakeBackend, config: VibeConfig
    ) -> None:
        """Test cycling through modes updates system prompt correctly."""
        agent = Agent(config=config, backend=backend, mode=AgentMode.INTERACTIVE)

        # INTERACTIVE -> AUTO_APPROVE (no plan mode text)
        agent.mode = AgentMode.AUTO_APPROVE
        assert "PLAN MODE" not in agent.messages[0].content

        # AUTO_APPROVE -> PLAN (plan mode text added)
        agent.mode = AgentMode.PLAN
        assert "PLAN MODE" in agent.messages[0].content

        # PLAN -> INTERACTIVE (plan mode text removed)
        agent.mode = AgentMode.INTERACTIVE
        assert "PLAN MODE" not in agent.messages[0].content
