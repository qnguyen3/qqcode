from __future__ import annotations

import asyncio
from enum import StrEnum, auto
import os
import subprocess
from typing import Any, ClassVar, assert_never

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.events import MouseUp
from textual.widget import Widget
from textual.widgets import Static

from vibe.cli.clipboard import copy_selection_to_clipboard
from vibe.cli.commands import CommandRegistry
from vibe.cli.textual_ui.handlers.event_handler import EventHandler
from vibe.cli.textual_ui.widgets.approval_app import ApprovalApp
from vibe.cli.textual_ui.widgets.chat_input import ChatInputContainer
from vibe.cli.textual_ui.widgets.compact import CompactMessage
from vibe.cli.textual_ui.widgets.config_app import ConfigApp
from vibe.cli.textual_ui.widgets.context_progress import ContextProgress, TokenState
from vibe.cli.textual_ui.widgets.loading import LoadingWidget
from vibe.cli.textual_ui.widgets.messages import (
    AssistantMessage,
    BashOutputMessage,
    ErrorMessage,
    InterruptMessage,
    UserCommandMessage,
    UserMessage,
)
from vibe.cli.textual_ui.widgets.mode_indicator import ModeIndicator
from vibe.cli.textual_ui.widgets.path_display import PathDisplay
from vibe.cli.textual_ui.widgets.plan_approval import PlanApprovalWidget
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage, ToolResultMessage
from vibe.cli.textual_ui.widgets.welcome import WelcomeBanner
from vibe.cli.update_notifier import (
    GitHubVersionUpdateGateway,
    VersionUpdate,
    VersionUpdateError,
    is_version_update_available,
)
from vibe.cli.update_notifier.version_update_gateway import VersionUpdateGateway
from vibe.core import __version__ as CORE_VERSION
from vibe.core.agent import Agent
from vibe.core.autocompletion.path_prompt_adapter import render_path_prompt
from vibe.core.config import HISTORY_FILE, VibeConfig
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    AgentMode,
    LLMMessage,
    ModeChangedEvent,
    ResumeSessionInfo,
    Role,
)
from vibe.core.utils import (
    ApprovalResponse,
    CancellationReason,
    get_user_cancellation_message,
    is_dangerous_directory,
    logger,
)


class BottomApp(StrEnum):
    Approval = auto()
    Config = auto()
    Input = auto()
    PlanApproval = auto()


class VibeApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = "app.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "force_quit", "Quit", show=False),
        Binding("escape", "interrupt", "Interrupt", show=False, priority=True),
        Binding("ctrl+o", "toggle_tool", "Toggle Tool", show=False),
        Binding("shift+tab", "cycle_mode", "Cycle Mode", show=False, priority=True),
    ]

    def __init__(
        self,
        config: VibeConfig,
        auto_approve: bool = False,
        agent_mode: AgentMode | None = None,
        enable_streaming: bool = False,
        initial_prompt: str | None = None,
        loaded_messages: list[LLMMessage] | None = None,
        session_info: ResumeSessionInfo | None = None,
        version_update_notifier: VersionUpdateGateway | None = None,
        current_version: str = CORE_VERSION,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        # Support both agent_mode and legacy auto_approve parameter
        if agent_mode is not None:
            self.agent_mode = agent_mode
        elif auto_approve:
            self.agent_mode = AgentMode.AUTO_APPROVE
        else:
            self.agent_mode = AgentMode.INTERACTIVE
        self.enable_streaming = enable_streaming
        self.agent: Agent | None = None
        self._agent_running = False
        self._agent_initializing = False
        self._interrupt_requested = False
        self._agent_task: asyncio.Task | None = None

        self._loading_widget: LoadingWidget | None = None
        self._pending_approval: asyncio.Future | None = None

        self.event_handler: EventHandler | None = None
        self.commands = CommandRegistry()

        self._chat_input_container: ChatInputContainer | None = None
        self._mode_indicator: ModeIndicator | None = None
        self._context_progress: ContextProgress | None = None
        self._current_bottom_app: BottomApp = BottomApp.Input
        self.theme = config.textual_theme

        self.history_file = HISTORY_FILE

        self._tools_collapsed = True
        self._current_streaming_message: AssistantMessage | None = None
        self._version_update_notifier = version_update_notifier
        self._is_update_check_enabled = config.enable_update_checks
        self._current_version = current_version
        self._update_notification_task: asyncio.Task | None = None
        self._update_notification_shown = False

        self._initial_prompt = initial_prompt
        self._loaded_messages = loaded_messages
        self._session_info = session_info
        self._agent_init_task: asyncio.Task | None = None
        # prevent a race condition where the agent initialization
        # completes exactly at the moment the user interrupts
        self._agent_init_interrupted = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat"):
            yield WelcomeBanner(self.config)
            yield Static(id="messages")

        with Horizontal(id="loading-area"):
            yield Static(id="loading-area-content")
            yield ModeIndicator(mode=self.agent_mode)

        with Static(id="bottom-app-container"):
            yield ChatInputContainer(
                history_file=self.history_file,
                command_registry=self.commands,
                id="input-container",
                show_warning=self.agent_mode,
            )

        with Horizontal(id="bottom-bar"):
            yield PathDisplay(
                self.config.displayed_workdir or self.config.effective_workdir
            )
            yield Static(id="spacer")
            yield ContextProgress()

    async def on_mount(self) -> None:
        self.event_handler = EventHandler(
            mount_callback=self._mount_and_scroll,
            scroll_callback=self._scroll_to_bottom_deferred,
            get_tools_collapsed=lambda: self._tools_collapsed,
        )

        self._chat_input_container = self.query_one(ChatInputContainer)
        self._mode_indicator = self.query_one(ModeIndicator)
        self._context_progress = self.query_one(ContextProgress)

        context_limit = self.config.get_context_limit_for_active_model()
        if context_limit > 0:
            self._context_progress.tokens = TokenState(
                max_tokens=context_limit, current_tokens=0
            )

        chat_input_container = self.query_one(ChatInputContainer)
        chat_input_container.focus_input()
        await self._show_dangerous_directory_warning()
        self._schedule_update_notification()

        if self._session_info:
            await self._mount_and_scroll(AssistantMessage(self._session_info.message()))

        if self._initial_prompt:
            self.call_after_refresh(self._process_initial_prompt)
        else:
            self._ensure_agent_init_task()

    def _process_initial_prompt(self) -> None:
        if self._initial_prompt:
            self.run_worker(
                self._handle_user_message(self._initial_prompt), exclusive=False
            )

    async def on_chat_input_container_submitted(
        self, event: ChatInputContainer.Submitted
    ) -> None:
        value = event.value.strip()
        if not value:
            return

        input_widget = self.query_one(ChatInputContainer)
        input_widget.value = ""

        if self._agent_running:
            await self._interrupt_agent()

        if value.startswith("!"):
            await self._handle_bash_command(value[1:])
            return

        if await self._handle_command(value):
            return

        await self._handle_user_message(value)

    async def on_approval_app_approval_granted(
        self, message: ApprovalApp.ApprovalGranted
    ) -> None:
        if self._pending_approval and not self._pending_approval.done():
            self._pending_approval.set_result((ApprovalResponse.YES, None))

        await self._switch_to_input_app()

    async def on_approval_app_approval_granted_always_tool(
        self, message: ApprovalApp.ApprovalGrantedAlwaysTool
    ) -> None:
        self._set_tool_permission_always(
            message.tool_name, save_permanently=message.save_permanently
        )

        if self._pending_approval and not self._pending_approval.done():
            self._pending_approval.set_result((ApprovalResponse.YES, None))

        await self._switch_to_input_app()

    async def on_approval_app_approval_rejected(
        self, message: ApprovalApp.ApprovalRejected
    ) -> None:
        if self._pending_approval and not self._pending_approval.done():
            feedback = str(
                get_user_cancellation_message(CancellationReason.OPERATION_CANCELLED)
            )
            self._pending_approval.set_result((ApprovalResponse.NO, feedback))

        await self._switch_to_input_app()

        if self._loading_widget and self._loading_widget.parent:
            await self._remove_loading_widget()

    async def on_plan_approval_widget_plan_approved(
        self, message: PlanApprovalWidget.PlanApproved
    ) -> None:
        """Handle plan approval - resolve Future so agent continues."""
        # Resolve the Future - agent continues with this result
        if hasattr(self, "_pending_plan_approval") and self._pending_plan_approval:
            self._pending_plan_approval.set_result((True, message.mode.value))

        # Update app mode for execution phase
        self.agent_mode = message.mode
        # First switch back to input (mounts ChatInputContainer)
        await self._switch_to_input_app()
        # Then sync mode to UI and agent (ChatInputContainer now exists)
        self._sync_mode_to_ui()
        self._sync_mode_to_agent()

    async def on_plan_approval_widget_revision_requested(
        self, message: PlanApprovalWidget.RevisionRequested
    ) -> None:
        """Handle revision request - resolve Future with feedback."""
        # Resolve with rejection + feedback - agent knows what to revise
        if hasattr(self, "_pending_plan_approval") and self._pending_plan_approval:
            feedback = getattr(message, "feedback", "Please revise the plan.")
            self._pending_plan_approval.set_result((False, feedback))
        await self._switch_to_input_app()

    async def _switch_to_plan_approval(self) -> None:
        """Switch to the plan approval widget."""
        bottom_container = self.query_one("#bottom-app-container", Static)

        try:
            existing = bottom_container.query_one(PlanApprovalWidget)
            if existing:
                existing.focus()
                return
        except Exception:
            pass

        # Remove existing widgets
        await bottom_container.remove_children()

        # Pass plan content to widget if available
        plan_content = getattr(self, "_pending_plan_content", None)
        plan_approval = PlanApprovalWidget(plan=plan_content)
        await bottom_container.mount(plan_approval)
        self._current_bottom_app = BottomApp.PlanApproval
        plan_approval.focus()

    async def _remove_loading_widget(self) -> None:
        if self._loading_widget and self._loading_widget.parent:
            await self._loading_widget.remove()
            self._loading_widget = None

    def on_config_app_setting_changed(self, message: ConfigApp.SettingChanged) -> None:
        if message.key == "textual_theme":
            self.theme = message.value

    async def on_config_app_config_closed(
        self, message: ConfigApp.ConfigClosed
    ) -> None:
        if message.changes:
            self._save_config_changes(message.changes)
            await self._reload_config()
        else:
            await self._mount_and_scroll(
                UserCommandMessage("Configuration closed (no changes saved).")
            )

        await self._switch_to_input_app()

    def _set_tool_permission_always(
        self, tool_name: str, save_permanently: bool = False
    ) -> None:
        if save_permanently:
            VibeConfig.save_updates({"tools": {tool_name: {"permission": "always"}}})

        if tool_name not in self.config.tools:
            self.config.tools[tool_name] = BaseToolConfig()

        self.config.tools[tool_name].permission = ToolPermission.ALWAYS

    def _save_config_changes(self, changes: dict[str, str]) -> None:
        if not changes:
            return

        updates: dict = {}

        for key, value in changes.items():
            match key:
                case "active_model":
                    if value != self.config.active_model:
                        updates["active_model"] = value
                case "textual_theme":
                    if value != self.config.textual_theme:
                        updates["textual_theme"] = value
                case "active_provider":
                    # Provider is implied by the chosen model and is not persisted.
                    pass

        if updates:
            VibeConfig.save_updates(updates)

    async def _handle_command(self, user_input: str) -> bool:
        if command := self.commands.find_command(user_input):
            handler = getattr(self, command.handler)
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
            return True
        return False

    async def _handle_bash_command(self, command: str) -> None:
        if not command:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No command provided after '!'", collapsed=self._tools_collapsed
                )
            )
            return

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=False,
                timeout=30,
                cwd=self.config.effective_workdir,
            )
            stdout = (
                result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            )
            stderr = (
                result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            )
            output = stdout or stderr or "(no output)"
            exit_code = result.returncode
            await self._mount_and_scroll(
                BashOutputMessage(
                    command, str(self.config.effective_workdir), output, exit_code
                )
            )
        except subprocess.TimeoutExpired:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Command timed out after 30 seconds",
                    collapsed=self._tools_collapsed,
                )
            )
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(f"Command failed: {e}", collapsed=self._tools_collapsed)
            )

    async def _handle_user_message(self, message: str) -> None:
        init_task = self._ensure_agent_init_task()
        pending_init = bool(init_task and not init_task.done())
        user_message = UserMessage(message, pending=pending_init)

        await self._mount_and_scroll(user_message)

        self.run_worker(
            self._process_user_message_after_mount(
                message=message,
                user_message=user_message,
                init_task=init_task,
                pending_init=pending_init,
            ),
            exclusive=False,
        )

    async def _process_user_message_after_mount(
        self,
        message: str,
        user_message: UserMessage,
        init_task: asyncio.Task | None,
        pending_init: bool,
    ) -> None:
        try:
            if init_task and not init_task.done():
                loading = LoadingWidget()
                self._loading_widget = loading
                await self.query_one("#loading-area-content").mount(loading)

                try:
                    await init_task
                finally:
                    if self._loading_widget and self._loading_widget.parent:
                        await self._loading_widget.remove()
                        self._loading_widget = None
                    if pending_init:
                        await user_message.set_pending(False)
            elif pending_init:
                await user_message.set_pending(False)

            if pending_init and self._agent_init_interrupted:
                self._agent_init_interrupted = False
                return

            if self.agent and not self._agent_running:
                self._agent_task = asyncio.create_task(self._handle_agent_turn(message))
        except asyncio.CancelledError:
            self._agent_init_interrupted = False
            if pending_init:
                await user_message.set_pending(False)
            return

    async def _initialize_agent(self) -> None:
        if self.agent or self._agent_initializing:
            return

        self._agent_initializing = True
        try:
            agent = Agent(
                self.config,
                mode=self.agent_mode,
                enable_streaming=self.enable_streaming,
            )

            if self.agent_mode != AgentMode.AUTO_APPROVE:
                agent.approval_callback = self._approval_callback

            # Always set plan approval callback for plan mode
            agent.plan_approval_callback = self._plan_approval_callback

            if self._loaded_messages:
                non_system_messages = [
                    msg
                    for msg in self._loaded_messages
                    if not (msg.role == Role.system)
                ]
                agent.messages.extend(non_system_messages)
                logger.info(
                    "Loaded %d messages from previous session", len(non_system_messages)
                )

            self.agent = agent
        except asyncio.CancelledError:
            self.agent = None
            return
        except Exception as e:
            self.agent = None
            await self._mount_and_scroll(
                ErrorMessage(str(e), collapsed=self._tools_collapsed)
            )
        finally:
            self._agent_initializing = False
            self._agent_init_task = None

    def _ensure_agent_init_task(self) -> asyncio.Task | None:
        if self.agent:
            self._agent_init_task = None
            self._agent_init_interrupted = False
            return None

        if self._agent_init_task and self._agent_init_task.done():
            if self._agent_init_task.cancelled():
                self._agent_init_task = None

        if not self._agent_init_task or self._agent_init_task.done():
            self._agent_init_interrupted = False
            self._agent_init_task = asyncio.create_task(self._initialize_agent())

        return self._agent_init_task

    async def _approval_callback(
        self, tool: str, args: dict, tool_call_id: str
    ) -> tuple[str, str | None]:
        self._pending_approval = asyncio.Future()
        await self._switch_to_approval_app(tool, args)
        result = await self._pending_approval
        self._pending_approval = None
        return result

    async def _plan_approval_callback(self, plan: str) -> tuple[bool, str | None]:
        """Callback invoked when agent calls submit_plan. Shows approval UI and waits."""
        self._pending_plan_approval: asyncio.Future[
            tuple[bool, str | None]
        ] | None = asyncio.Future()
        self._pending_plan_content: str | None = plan
        await self._switch_to_plan_approval()
        result = await self._pending_plan_approval
        self._pending_plan_approval = None
        self._pending_plan_content = None
        return result

    async def _handle_agent_turn(self, prompt: str) -> None:
        if not self.agent:
            return

        self._agent_running = True

        loading_area = self.query_one("#loading-area-content")

        loading = LoadingWidget()
        self._loading_widget = loading
        await loading_area.mount(loading)

        try:
            rendered_prompt = render_path_prompt(
                prompt, base_dir=self.config.effective_workdir
            )
            async for event in self.agent.act(rendered_prompt):
                if self._context_progress and self.agent:
                    current_state = self._context_progress.tokens
                    self._context_progress.tokens = TokenState(
                        max_tokens=current_state.max_tokens,
                        current_tokens=self.agent.stats.context_tokens,
                    )

                # Handle mode change events (legacy, for backward compatibility)
                if isinstance(event, ModeChangedEvent):
                    self.agent_mode = event.new_mode
                    self._sync_mode_to_ui()
                    continue

                if self.event_handler:
                    await self.event_handler.handle_event(
                        event,
                        loading_active=self._loading_widget is not None,
                        loading_widget=self._loading_widget,
                    )

        except asyncio.CancelledError:
            if self._loading_widget and self._loading_widget.parent:
                await self._loading_widget.remove()
            if self.event_handler:
                self.event_handler.stop_current_tool_call()
            raise
        except Exception as e:
            if self._loading_widget and self._loading_widget.parent:
                await self._loading_widget.remove()
            if self.event_handler:
                self.event_handler.stop_current_tool_call()
            await self._mount_and_scroll(
                ErrorMessage(str(e), collapsed=self._tools_collapsed)
            )
        finally:
            self._agent_running = False
            self._interrupt_requested = False
            self._agent_task = None
            if self._loading_widget:
                await self._loading_widget.remove()
            self._loading_widget = None
            await self._finalize_current_streaming_message()

            # Note: Plan approval is now handled via callback during submit_plan tool execution,
            # not after agent turn ends.

    async def _interrupt_agent(self) -> None:
        interrupting_agent_init = bool(
            self._agent_init_task and not self._agent_init_task.done()
        )

        if (
            not self._agent_running and not interrupting_agent_init
        ) or self._interrupt_requested:
            return

        self._interrupt_requested = True

        if interrupting_agent_init and self._agent_init_task:
            self._agent_init_interrupted = True
            self._agent_init_task.cancel()
            try:
                await self._agent_init_task
            except asyncio.CancelledError:
                pass

        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass

        if self.event_handler:
            self.event_handler.stop_current_tool_call()
            self.event_handler.stop_current_compact()

        self._agent_running = False
        loading_area = self.query_one("#loading-area-content")
        await loading_area.remove_children()

        await self._finalize_current_streaming_message()
        await self._mount_and_scroll(InterruptMessage())

        self._interrupt_requested = False

    async def _show_help(self) -> None:
        help_text = self.commands.get_help_text()
        await self._mount_and_scroll(UserCommandMessage(help_text))

    async def _show_status(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        stats = self.agent.stats
        status_text = f"""## Agent Statistics

- **Steps**: {stats.steps:,}
- **Session Prompt Tokens**: {stats.session_prompt_tokens:,}
- **Session Completion Tokens**: {stats.session_completion_tokens:,}
- **Session Total LLM Tokens**: {stats.session_total_llm_tokens:,}
- **Last Turn Tokens**: {stats.last_turn_total_tokens:,}
- **Cost**: ${stats.session_cost:.4f}
"""
        await self._mount_and_scroll(UserCommandMessage(status_text))

    async def _show_config(self) -> None:
        """Switch to the configuration app in the bottom panel."""
        if self._current_bottom_app == BottomApp.Config:
            return
        await self._switch_to_config_app()

    async def _reload_config(self) -> None:
        try:
            new_config = VibeConfig.load()

            if self.agent:
                await self.agent.reload_with_initial_messages(config=new_config)

            self.config = new_config
            if self._context_progress:
                context_limit = self.config.get_context_limit_for_active_model()
                if context_limit > 0:
                    current_tokens = (
                        self.agent.stats.context_tokens if self.agent else 0
                    )
                    self._context_progress.tokens = TokenState(
                        max_tokens=context_limit,
                        current_tokens=current_tokens,
                    )
                else:
                    self._context_progress.tokens = TokenState()

            await self._mount_and_scroll(UserCommandMessage("Configuration reloaded."))
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to reload config: {e}", collapsed=self._tools_collapsed
                )
            )

    async def _clear_history(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No conversation history to clear yet.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not self.agent:
            return

        try:
            await self.agent.clear_history()
            await self._finalize_current_streaming_message()
            messages_area = self.query_one("#messages")
            await messages_area.remove_children()

            if self._context_progress and self.agent:
                current_state = self._context_progress.tokens
                self._context_progress.tokens = TokenState(
                    max_tokens=current_state.max_tokens,
                    current_tokens=self.agent.stats.context_tokens,
                )
            await self._mount_and_scroll(
                UserCommandMessage("Conversation history cleared!")
            )
            chat = self.query_one("#chat", VerticalScroll)
            chat.scroll_home(animate=False)

        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to clear history: {e}", collapsed=self._tools_collapsed
                )
            )

    async def _show_log_path(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No log file created yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not self.agent.interaction_logger.enabled:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Session logging is disabled in configuration.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        try:
            log_path = str(self.agent.interaction_logger.filepath)
            await self._mount_and_scroll(
                UserCommandMessage(
                    f"## Current Log File Path\n\n`{log_path}`\n\nYou can send this file to share your interaction."
                )
            )
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to get log path: {e}", collapsed=self._tools_collapsed
                )
            )

    async def _compact_history(self) -> None:
        if self._agent_running:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Cannot compact while agent is processing. Please wait.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No conversation history to compact yet.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if len(self.agent.messages) <= 1:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No conversation history to compact yet.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not self.agent or not self.event_handler:
            return

        old_tokens = self.agent.stats.context_tokens
        compact_msg = CompactMessage()
        self.event_handler.current_compact = compact_msg
        await self._mount_and_scroll(compact_msg)

        try:
            await self.agent.compact()
            new_tokens = self.agent.stats.context_tokens
            compact_msg.set_complete(old_tokens=old_tokens, new_tokens=new_tokens)
            self.event_handler.current_compact = None

            if self._context_progress:
                current_state = self._context_progress.tokens
                self._context_progress.tokens = TokenState(
                    max_tokens=current_state.max_tokens, current_tokens=new_tokens
                )
        except Exception as e:
            compact_msg.set_error(str(e))
            self.event_handler.current_compact = None

    async def _exit_app(self) -> None:
        self.exit()

    async def _switch_to_config_app(self) -> None:
        if self._current_bottom_app == BottomApp.Config:
            return

        bottom_container = self.query_one("#bottom-app-container")
        await self._mount_and_scroll(UserCommandMessage("Configuration opened..."))

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            await chat_input_container.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = False

        config_app = ConfigApp(self.config)
        await bottom_container.mount(config_app)
        self._current_bottom_app = BottomApp.Config

        self.call_after_refresh(config_app.focus)

    async def _switch_to_approval_app(self, tool_name: str, tool_args: dict) -> None:
        bottom_container = self.query_one("#bottom-app-container")

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            await chat_input_container.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = False

        approval_app = ApprovalApp(
            tool_name=tool_name,
            tool_args=tool_args,
            workdir=str(self.config.effective_workdir),
            config=self.config,
        )
        await bottom_container.mount(approval_app)
        self._current_bottom_app = BottomApp.Approval

        self.call_after_refresh(approval_app.focus)
        self.call_after_refresh(self._scroll_to_bottom)

    async def _switch_to_input_app(self) -> None:
        bottom_container = self.query_one("#bottom-app-container")

        try:
            config_app = self.query_one("#config-app")
            await config_app.remove()
        except Exception:
            pass

        try:
            approval_app = self.query_one("#approval-app")
            await approval_app.remove()
        except Exception:
            pass

        try:
            plan_approval = self.query_one("#plan-approval-app")
            await plan_approval.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = True

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            self._chat_input_container = chat_input_container
            self._current_bottom_app = BottomApp.Input
            self.call_after_refresh(chat_input_container.focus_input)
            return
        except Exception:
            pass

        chat_input_container = ChatInputContainer(
            history_file=self.history_file,
            command_registry=self.commands,
            id="input-container",
            show_warning=self.agent_mode,
        )
        await bottom_container.mount(chat_input_container)
        self._chat_input_container = chat_input_container

        self._current_bottom_app = BottomApp.Input

        self.call_after_refresh(chat_input_container.focus_input)

    def _focus_current_bottom_app(self) -> None:
        try:
            match self._current_bottom_app:
                case BottomApp.Input:
                    self.query_one(ChatInputContainer).focus_input()
                case BottomApp.Config:
                    self.query_one(ConfigApp).focus()
                case BottomApp.Approval:
                    self.query_one(ApprovalApp).focus()
                case BottomApp.PlanApproval:
                    self.query_one(PlanApprovalWidget).focus()
                case app:
                    assert_never(app)
        except Exception:
            pass

    def action_interrupt(self) -> None:
        if self._current_bottom_app == BottomApp.Config:
            try:
                config_app = self.query_one(ConfigApp)
                config_app.action_close()
            except Exception:
                pass
            return

        if self._current_bottom_app == BottomApp.Approval:
            try:
                approval_app = self.query_one(ApprovalApp)
                approval_app.action_reject()
            except Exception:
                pass
            return

        has_pending_user_message = any(
            msg.has_class("pending") for msg in self.query(UserMessage)
        )

        interrupt_needed = self._agent_running or (
            self._agent_init_task
            and not self._agent_init_task.done()
            and has_pending_user_message
        )

        if interrupt_needed:
            self.run_worker(self._interrupt_agent(), exclusive=False)

        self._scroll_to_bottom()
        self._focus_current_bottom_app()

    async def action_toggle_tool(self) -> None:
        if not self.event_handler:
            return

        self._tools_collapsed = not self._tools_collapsed

        for result in self.event_handler.tool_results:
            result.collapsed = self._tools_collapsed
            await result.render_result()

        try:
            error_messages = self.query(ErrorMessage)
            for error_msg in error_messages:
                error_msg.set_collapsed(self._tools_collapsed)
        except Exception:
            pass

    def action_cycle_mode(self) -> None:
        if self._current_bottom_app != BottomApp.Input:
            return

        # Cycle through modes: INTERACTIVE -> AUTO_APPROVE -> PLAN -> INTERACTIVE
        modes = [AgentMode.INTERACTIVE, AgentMode.AUTO_APPROVE, AgentMode.PLAN]
        current_idx = modes.index(self.agent_mode)
        self.agent_mode = modes[(current_idx + 1) % len(modes)]

        self._sync_mode_to_ui()
        self._sync_mode_to_agent()
        self._focus_current_bottom_app()

    def _sync_mode_to_ui(self) -> None:
        """Sync agent mode to UI components."""
        if self._mode_indicator:
            self._mode_indicator.set_mode(self.agent_mode)

        if self._chat_input_container:
            self._chat_input_container.set_show_warning(self.agent_mode)

    def _sync_mode_to_agent(self) -> None:
        """Sync agent mode to the Agent instance."""
        if not self.agent:
            return

        self.agent.mode = self.agent_mode

        # Set approval callback based on mode
        if self.agent_mode == AgentMode.AUTO_APPROVE:
            self.agent.approval_callback = None
        else:
            self.agent.approval_callback = self._approval_callback

    def action_force_quit(self) -> None:
        input_widgets = self.query(ChatInputContainer)
        if input_widgets:
            input_widget = input_widgets.first()
            if input_widget.value:
                input_widget.value = ""
                return

        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()

        self.exit()

    async def _show_dangerous_directory_warning(self) -> None:
        is_dangerous, reason = is_dangerous_directory()
        if is_dangerous:
            warning = (
                f"⚠️ WARNING: {reason}\n\nRunning in this location is not recommended."
            )
            await self._mount_and_scroll(UserCommandMessage(warning))

    async def _finalize_current_streaming_message(self) -> None:
        if self._current_streaming_message is None:
            return

        await self._current_streaming_message.stop_stream()
        self._current_streaming_message = None

    async def _mount_and_scroll(self, widget: Widget) -> None:
        messages_area = self.query_one("#messages")
        chat = self.query_one("#chat", VerticalScroll)
        was_at_bottom = self._is_scrolled_to_bottom(chat)

        if isinstance(widget, AssistantMessage):
            if self._current_streaming_message is not None:
                # Append reasoning content if present
                reasoning = widget._reasoning_content or ""
                if reasoning:
                    await self._current_streaming_message.append_reasoning_content(
                        reasoning
                    )
                # Append regular content if present
                content = widget._content or ""
                if content:
                    await self._current_streaming_message.append_content(content)
            else:
                self._current_streaming_message = widget
                await messages_area.mount(widget)
                await widget.write_initial_content()
        else:
            await self._finalize_current_streaming_message()
            await messages_area.mount(widget)

            is_tool_message = isinstance(widget, (ToolCallMessage, ToolResultMessage))

            if not is_tool_message:
                self.call_after_refresh(self._scroll_to_bottom)

        if was_at_bottom:
            self.call_after_refresh(self._anchor_if_scrollable)

    def _is_scrolled_to_bottom(self, scroll_view: VerticalScroll) -> bool:
        try:
            threshold = 3
            return scroll_view.scroll_y >= (scroll_view.max_scroll_y - threshold)
        except Exception:
            return True

    def _scroll_to_bottom(self) -> None:
        try:
            chat = self.query_one("#chat")
            chat.scroll_end(animate=False)
        except Exception:
            pass

    def _scroll_to_bottom_deferred(self) -> None:
        self.call_after_refresh(self._scroll_to_bottom)

    def _anchor_if_scrollable(self) -> None:
        try:
            chat = self.query_one("#chat", VerticalScroll)
            if chat.max_scroll_y == 0:
                return
            chat.anchor()
        except Exception:
            pass

    def _schedule_update_notification(self) -> None:
        if (
            self._version_update_notifier is None
            or self._update_notification_task
            or not self._is_update_check_enabled
        ):
            return

        self._update_notification_task = asyncio.create_task(
            self._check_version_update(), name="version-update-check"
        )

    async def _check_version_update(self) -> None:
        try:
            if self._version_update_notifier is None:
                return

            update = await is_version_update_available(
                self._version_update_notifier, current_version=self._current_version
            )
        except VersionUpdateError as error:
            self.notify(
                error.message,
                title="Update check failed",
                severity="warning",
                timeout=10,
            )
            return
        except Exception as exc:
            logger.debug("Version update check failed", exc_info=exc)
            return
        finally:
            self._update_notification_task = None

        if update is None:
            return

        self._display_update_notification(update)

    def _display_update_notification(self, update: VersionUpdate) -> None:
        if self._update_notification_shown:
            return

        message = f'{self._current_version} => {update.latest_version}\nRun "uv tool upgrade qqcode" to update'

        self.notify(
            message, title="Update available", severity="information", timeout=10
        )
        self._update_notification_shown = True

    def on_mouse_up(self, event: MouseUp) -> None:
        copy_selection_to_clipboard(self)


def run_textual_ui(
    config: VibeConfig,
    auto_approve: bool = False,
    enable_streaming: bool = False,
    initial_prompt: str | None = None,
    loaded_messages: list[LLMMessage] | None = None,
    session_info: ResumeSessionInfo | None = None,
) -> None:
    update_notifier = GitHubVersionUpdateGateway(
        owner="qnguyen3", repository="qqcode", token=os.getenv("GITHUB_TOKEN")
    )
    app = VibeApp(
        config=config,
        auto_approve=auto_approve,
        enable_streaming=enable_streaming,
        initial_prompt=initial_prompt,
        loaded_messages=loaded_messages,
        session_info=session_info,
        version_update_notifier=update_notifier,
    )
    app.run()
