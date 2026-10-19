from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum, auto
import os
from pathlib import Path
import subprocess
from typing import Any, ClassVar, assert_never
from uuid import uuid4

from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import BindingsMap, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.events import MouseUp
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from vibe.cli.clipboard import copy_selection_to_clipboard, copy_text_to_clipboard
from vibe.cli.commands import CommandRegistry
from vibe.cli.keybindings import (
    KEY_ACTIONS,
    build_bindings,
    describe_actions,
    ensure_keybindings_file,
    format_key_display,
    keybinding_files,
    load_overrides,
)
from vibe.cli.textual_ui.handlers.event_handler import EventHandler
from vibe.cli.textual_ui.widgets.approval_app import ApprovalApp
from vibe.cli.textual_ui.widgets.chat_input import ChatInputContainer
from vibe.cli.textual_ui.widgets.compact import CompactMessage
from vibe.cli.textual_ui.widgets.config_app import ConfigApp, ScopedModelsApp
from vibe.cli.textual_ui.widgets.context_progress import ContextProgress, TokenState
from vibe.cli.textual_ui.widgets.conversations_app import ConversationsApp, ForkApp
from vibe.cli.textual_ui.widgets.history_message import (
    HistoricalToolCall,
    HistoricalToolResult,
)
from vibe.cli.textual_ui.widgets.loading import LoadingWidget
from vibe.cli.textual_ui.widgets.login_screen import (
    LoginDialogScreen,
    LoginResult,
    LoginScreen,
    SecretScreen,
    SelectorEntry,
)
from vibe.cli.textual_ui.widgets.messages import (
    AssistantMessage,
    BashOutputMessage,
    ErrorMessage,
    InterruptMessage,
    UserCommandMessage,
    UserMessage,
)
from vibe.cli.textual_ui.widgets.mode_indicator import ModeIndicator
from vibe.cli.textual_ui.widgets.model_selector import ModelSelectorScreen
from vibe.cli.textual_ui.widgets.path_display import PathDisplay
from vibe.cli.textual_ui.widgets.plan_approval import PlanApprovalWidget
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage, ToolResultMessage
from vibe.cli.textual_ui.widgets.trust_app import TrustScreen
from vibe.cli.textual_ui.widgets.welcome import WelcomeBanner
from vibe.cli.update_notifier import (
    GitHubVersionUpdateGateway,
    VersionUpdate,
    VersionUpdateError,
    is_version_update_available,
)
from vibe.cli.update_notifier.version_update_gateway import VersionUpdateGateway
from vibe.core import __version__ as CORE_VERSION
from vibe.core.agent import Agent, context_overview
from vibe.core.ambient import ambient_setup
from vibe.core.auth import (
    AuthKind,
    ProviderAuth,
    all_provider_auths,
    configured_provider_names,
    find_provider,
    logout,
    provider_auths,
    save_api_key,
)
from vibe.core.autocompletion.path_prompt_adapter import render_path_prompt
from vibe.core.config import (
    HISTORY_FILE,
    ThinkingLevel,
    VibeConfig,
    get_vibe_home,
    save_oauth_token,
    supported_thinking_levels,
)
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.mcp.manager import MCPManager
from vibe.core.oauth import UnknownOAuthFlowError, get_flow
from vibe.core.packages import package_resource_dirs
from vibe.core.prompts.templates import load_templates, render_template
from vibe.core.sessions.entries import MessageEntry
from vibe.core.sessions.export import export_session, import_session
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.skills.manager import SkillManager, SkillNotFoundError
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.trust import ProjectResources, TrustChoice, TrustDecision, TrustStore
from vibe.core.types import (
    AgentMode,
    AgentStats,
    BaseEvent,
    ContextUsageEvent,
    LLMMessage,
    ModeChangedEvent,
    ResumeSessionInfo,
    Role,
    SystemPromptChangedEvent,
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
    Conversations = auto()
    Fork = auto()
    Input = auto()
    PlanApproval = auto()
    ScopedModels = auto()


THINKING_LEVELS: tuple[ThinkingLevel, ...] = (
    ThinkingLevel.OFF,
    ThinkingLevel.MINIMAL,
    ThinkingLevel.LOW,
    ThinkingLevel.MEDIUM,
    ThinkingLevel.HIGH,
    ThinkingLevel.XHIGH,
    ThinkingLevel.MAX,
)


@dataclass(frozen=True)
class UISessionOptions:
    """Optional session state restored when the UI starts."""

    name: str | None = None
    thinking_level: ThinkingLevel | None = None
    trust_request: ProjectResources | None = None


def _stats_from_metadata(metadata: dict[str, Any]) -> AgentStats | None:
    raw = metadata.get("stats")
    if isinstance(raw, dict):
        try:
            return AgentStats.model_validate(raw)
        except Exception:
            return None
    return None


class VibeApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = "app.tcss"

    # Defaults live in the action registry; the instance rebuilds this map from
    # the user's overrides before the app starts.
    BINDINGS: ClassVar[list[BindingType]] = list(build_bindings({})[0])

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
        additional_directory_contexts: list[Path] | None = None,
        prompt_template_dirs: list[Path] | None = None,
        resume: UISessionOptions | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config
        self._keybinding_warnings: list[str] = []
        self._keybindings_watch_task: asyncio.Task[None] | None = None
        self._session_name = resume.name if resume else None
        self._initial_thinking_level = resume.thinking_level if resume else None
        self._trust_request = resume.trust_request if resume else None
        # Support both agent_mode and legacy auto_approve parameter
        if agent_mode is not None:
            self.agent_mode = agent_mode
        elif auto_approve:
            self.agent_mode = AgentMode.AUTO_APPROVE
        else:
            self.agent_mode = AgentMode.INTERACTIVE
        self.enable_streaming = enable_streaming
        self.agent: Agent | None = None
        self.mcp_manager: MCPManager | None = None
        self._agent_running = False
        self._agent_initializing = False
        self._interrupt_requested = False
        self._agent_task: asyncio.Task | None = None

        self._loading_widget: LoadingWidget | None = None
        self._pending_approval: asyncio.Future | None = None

        self.event_handler: EventHandler | None = None
        self.commands = CommandRegistry()
        self._chat_input_container: ChatInputContainer | None = None
        self._prompt_template_dirs = list(prompt_template_dirs or [])
        self.skill_manager = SkillManager(config)
        self.prompt_templates = load_templates(
            self._default_template_dirs(self._prompt_template_dirs)
        )
        self._register_dynamic_commands()

        self._mode_indicator: ModeIndicator | None = None
        self._context_progress: ContextProgress | None = None
        self._current_bottom_app: BottomApp = BottomApp.Input
        self.theme = config.textual_theme
        self._apply_keybindings()

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
        self._additional_directory_contexts = additional_directory_contexts
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
        if self._keybinding_warnings:
            await self._mount_and_scroll(
                UserCommandMessage(
                    "Keybinding warnings:\n"
                    + "\n".join(f"- {warning}" for warning in self._keybinding_warnings)
                )
            )
        self._start_keybindings_watcher()
        self._schedule_update_notification()

        if self._session_info:
            await self._mount_and_scroll(AssistantMessage(self._session_info.message()))

        if self._trust_request is not None:
            self.push_screen(
                TrustScreen(self._trust_request), callback=self._on_trust_resolved
            )
            return

        self._start_initial_prompt_or_agent()

    def _start_initial_prompt_or_agent(self) -> None:
        if self._initial_prompt:
            self.call_after_refresh(self._process_initial_prompt)
        else:
            self._ensure_agent_init_task()

    def _on_trust_resolved(self, choice: TrustChoice | None) -> None:
        self.run_worker(self._apply_trust_choice(choice), exclusive=False)

    async def _apply_trust_choice(self, choice: TrustChoice | None) -> None:
        project = self._trust_request
        self._trust_request = None
        if project is None:
            return

        match choice:
            case TrustChoice.ALWAYS:
                TrustStore.load().set(project.project_dir, TrustDecision.TRUSTED)
                trusted = True
                message = (
                    f"Project trusted: loading resources from {project.project_dir}."
                )
            case TrustChoice.YES:
                trusted = True
                message = (
                    f"Project trusted for this session: "
                    f"loading resources from {project.project_dir}."
                )
            case _:
                trusted = False
                message = "Project not trusted: using global configuration only."

        try:
            new_config = VibeConfig.load(trust=trusted)
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to load configuration: {e}",
                    collapsed=self._tools_collapsed,
                )
            )
        else:
            self.config = new_config
            self.skill_manager = SkillManager(new_config)
            self.prompt_templates = load_templates(
                self._default_template_dirs(self._prompt_template_dirs)
            )
            self._register_dynamic_commands()
            self._update_context_progress()
            await self._mount_and_scroll(UserCommandMessage(message))

        self._start_initial_prompt_or_agent()

    def _process_initial_prompt(self) -> None:
        if self._initial_prompt:
            self.run_worker(
                self._handle_user_message(self._initial_prompt), exclusive=False
            )

    async def on_unmount(self) -> None:
        if self._keybindings_watch_task is not None:
            self._keybindings_watch_task.cancel()
            self._keybindings_watch_task = None
        if self.mcp_manager is not None:
            await self.mcp_manager.stop()
            self.mcp_manager = None

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
        # Sync mode to UI after refresh cycle completes (children like #input-box need to be composed first)
        self.call_after_refresh(self._sync_mode_to_ui)
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

    async def on_conversations_app_conversation_selected(
        self, message: ConversationsApp.ConversationSelected
    ) -> None:
        """Handle selection of a past conversation."""
        await self._load_session_file(message.filepath)
        await self._switch_to_input_app()

    async def on_conversations_app_conversations_closed(
        self, message: ConversationsApp.ConversationsClosed
    ) -> None:
        """Handle closing the conversations dialog without selection."""
        await self._switch_to_input_app()

    async def on_scoped_models_app_scopes_saved(
        self, message: ScopedModelsApp.ScopesSaved
    ) -> None:
        scopes = list(message.scopes)
        VibeConfig.save_updates({"enabled_models": scopes})
        self.config.enabled_models = scopes
        scope_count = len(self.config.models_in_scope())
        await self._mount_and_scroll(
            UserCommandMessage(f"Model scope saved: {scope_count} model(s) selectable.")
        )
        await self._switch_to_input_app()

    async def on_fork_app_message_selected(
        self, message: ForkApp.MessageSelected
    ) -> None:
        try:
            if self.agent is None:
                raise RuntimeError("Agent is not initialized")
            filepath = self.agent.interaction_logger.filepath
            if filepath is None:
                raise RuntimeError("No session file to fork")
            store = JsonlSessionStore(self.config.session_logging)
            forked_path = store.fork(
                filepath, message.entry_id, workdir=Path(self.config.effective_workdir)
            )
            view = store.read(forked_path)
            await self.agent.switch_session(
                session_id=view.header.id,
                filepath=forked_path,
                messages=view.messages(),
                stats=view.stats(),
            )
            await self._finalize_current_streaming_message()
            await self._reset_ui_messages()
            await self._restore_ui_from_messages(view.messages())
            await self._mount_and_scroll(
                AssistantMessage(f"Forked into session `{view.header.id[:8]}`.")
            )
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to fork session: {e}", collapsed=self._tools_collapsed
                )
            )

        await self._switch_to_input_app()

    async def on_fork_app_fork_closed(self, message: ForkApp.ForkClosed) -> None:
        await self._switch_to_input_app()

    async def _load_session_file(self, filepath: Path) -> None:
        try:
            # Load the selected session
            loaded_messages, metadata = InteractionLogger.load_session(filepath)

            # Clear current messages area
            messages_area = self.query_one("#messages")
            await messages_area.remove_children()

            # Show session continuation message
            session_time = metadata.get("start_time", "unknown time")
            session_id = str(metadata.get("session_id", "unknown"))
            await self._mount_and_scroll(
                AssistantMessage(
                    f"Continuing session `{session_id[:8]}` from {session_time}"
                )
            )

            # Restore UI from the last few turns
            await self._restore_ui_from_messages(loaded_messages)

            # Ensure agent is initialized
            if not self.agent:
                await self._initialize_agent()

            if self.agent:
                self._restore_additional_directory_contexts(metadata)

                if self.config.session_logging.enabled:
                    await self.agent.switch_session(
                        session_id=session_id,
                        filepath=filepath,
                        messages=loaded_messages,
                        stats=_stats_from_metadata(metadata),
                    )
                else:
                    await self.agent.clear_history()
                    for message in [
                        m for m in loaded_messages if m.role != Role.system
                    ]:
                        self.agent.add_message(message)

                await self._apply_session_preferences(metadata)
                logger.info(
                    "Loaded %d messages from session %s",
                    len(loaded_messages),
                    session_id,
                )

            self._update_context_progress()

        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to load session: {e}", collapsed=self._tools_collapsed
                )
            )

    def _restore_additional_directory_contexts(self, metadata: dict[str, Any]) -> None:
        if not self.agent:
            return

        additional_contexts = metadata.get("additional_directory_contexts", [])
        if not additional_contexts:
            return

        for context_path_str in additional_contexts:
            try:
                context_path = Path(context_path_str)
                if context_path.exists() and context_path.is_dir():
                    self.agent.add_additional_directory_context(context_path)
            except Exception:
                # Skip invalid paths
                continue
        self.agent._regenerate_system_prompt_with_additional_contexts()

    async def _apply_session_preferences(self, metadata: dict[str, Any]) -> None:
        custom = metadata.get("custom")
        if not isinstance(custom, dict):
            custom = {}

        model_entry = custom.get("model")
        if isinstance(model_entry, dict) and isinstance(model_entry.get("alias"), str):
            await self._set_model_alias(model_entry["alias"], announce=False)

        raw_level: str | None = None
        level_entry = custom.get("thinking_level")
        if isinstance(level_entry, dict) and isinstance(level_entry.get("level"), str):
            raw_level = level_entry["level"]
        elif isinstance(metadata.get("thinking_level"), str):
            raw_level = metadata["thinking_level"]
        if raw_level is not None:
            try:
                level: ThinkingLevel | None = ThinkingLevel(raw_level)
            except ValueError:
                level = None
            await self._set_thinking_level(level, announce=False)

        name_entry = custom.get("name")
        if isinstance(name_entry, dict) and isinstance(name_entry.get("name"), str):
            self._session_name = name_entry["name"]

    async def _restore_ui_from_messages(
        self, messages: list[LLMMessage], max_messages: int = 15
    ) -> None:
        """Restore UI state from saved messages.

        Renders the last N messages to show conversation context.
        """
        # Skip system message and get last N messages
        non_system = [msg for msg in messages if msg.role != Role.system]
        recent_messages = non_system[-max_messages:] if non_system else []

        for msg in recent_messages:
            match msg.role:
                case Role.user:
                    if msg.content:
                        await self._mount_and_scroll(UserMessage(msg.content))

                case Role.assistant:
                    # Show assistant content if present
                    if msg.content:
                        assistant_msg = AssistantMessage(
                            msg.content, reasoning_content=msg.reasoning_content
                        )
                        await self._mount_and_scroll(assistant_msg)
                        await assistant_msg.write_initial_content()

                    # Show tool calls if present
                    if msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            if tool_call.function and tool_call.function.name:
                                await self._mount_and_scroll(
                                    HistoricalToolCall(
                                        tool_call.function.name,
                                        tool_call.function.arguments or "",
                                    )
                                )

                case Role.tool:
                    # Show tool result
                    tool_name = msg.name or "tool"
                    is_error = bool(
                        msg.content
                        and (
                            "error" in msg.content.lower()[:50]
                            or msg.content.startswith("Error:")
                        )
                    )
                    await self._mount_and_scroll(
                        HistoricalToolResult(
                            tool_name, msg.content or "", is_error=is_error
                        )
                    )

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

    def _default_template_dirs(self, extra_dirs: list[Path]) -> list[Path]:
        dirs: list[Path] = list(extra_dirs)
        cwd = self.config.effective_workdir
        for directory in (cwd, *cwd.parents):
            candidate = directory / ".qqcode" / "prompts" / "commands"
            if candidate.is_dir():
                dirs.append(candidate)
                break

        dirs.extend(package_resource_dirs(self.config.packages, "prompts"))

        global_dir = get_vibe_home() / "prompts" / "commands"
        if global_dir.is_dir():
            dirs.append(global_dir)
        return dirs

    def _apply_keybindings(self) -> None:
        """Rebuild the app bindings from the registry and the user overrides."""
        overrides, warnings = load_overrides(self.config)
        bindings, binding_warnings = build_bindings(overrides)
        self._bindings = BindingsMap(bindings)
        self._keybinding_warnings = [*warnings, *binding_warnings]

    def keybinding_help_lines(self) -> list[str]:
        """Current keys per action, for the help screen."""
        return describe_actions(self._effective_keybinding_keys())

    def _effective_keybinding_keys(self) -> dict[str, list[str]]:
        keys: dict[str, list[str]] = {}
        for key, bindings in self._bindings.key_to_bindings.items():
            for binding in bindings:
                keys.setdefault(binding.action, []).append(key)
        return keys

    def _hotkeys_text(self) -> str:
        effective = self._effective_keybinding_keys()
        lines = ["### Keybindings", ""]
        for action in KEY_ACTIONS:
            keys = effective.get(action.name, list(action.default))
            rendered = ", ".join(f"`{format_key_display(key)}`" for key in keys)
            changed = tuple(keys) != action.default
            custom = " (custom)" if changed else ""
            lines.append(f"- {rendered} {action.description}{custom}")

        if self._keybinding_warnings:
            lines.extend(["", "### Warnings", ""])
            lines.extend(f"- {warning}" for warning in self._keybinding_warnings)
        lines.extend(["", "Use `/hotkeys edit` to open the keybindings file."])
        return "\n".join(lines)

    async def _show_hotkeys(self, user_input: str = "") -> None:
        match user_input.split(maxsplit=1):
            case [_, "edit"]:
                await self._edit_keybindings()
            case _:
                await self._mount_and_scroll(UserCommandMessage(self._hotkeys_text()))

    async def _edit_keybindings(self) -> None:
        path = ensure_keybindings_file(self.config)
        self._start_keybindings_watcher()
        await self._mount_and_scroll(
            UserCommandMessage(
                f"Keybindings file: {path}\n"
                f"Edit it (for example `$EDITOR {path}`); changes reload "
                "automatically while QQcode is running."
            )
        )

    def _start_keybindings_watcher(self) -> None:
        if self._keybindings_watch_task is not None:
            return
        watched = [path for path in keybinding_files(self.config) if path.exists()]
        if not watched:
            return
        self._keybindings_watch_task = asyncio.create_task(
            self._watch_keybindings(watched)
        )

    async def _watch_keybindings(self, paths: list[Path]) -> None:
        from watchfiles import awatch

        try:
            async for _changes in awatch(*paths):
                if not self.is_running:
                    return
                self.run_worker(self._reload_keybindings(), exclusive=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Keybindings watcher stopped: %s", exc)

    async def _reload_keybindings(self) -> None:
        self._apply_keybindings()
        await self._mount_and_scroll(UserCommandMessage("Keybindings reloaded."))

    @staticmethod
    def _command_argument(user_input: str) -> str:
        parts = user_input.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _auth_detail(auth: ProviderAuth) -> str:
        status = f"✓ {auth.source}" if auth.source else "not configured"
        return f"{auth.label} · {status}"

    async def _login_command(self, user_input: str = "") -> None:
        """``/login [provider]``: sign in or store an API key."""
        self.run_worker(self._login_flow(user_input), exclusive=False, group="login")

    async def _login_flow(self, user_input: str) -> None:
        name = self._command_argument(user_input)
        if name:
            await self._login_named_provider(name)
            return
        await self._login_interactive()

    async def _login_named_provider(self, name: str) -> None:
        auths = provider_auths(self.config, name)
        if not auths:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Unknown provider '{name}'. Run /login to list the "
                    "configured providers.",
                    collapsed=self._tools_collapsed,
                )
            )
            return
        if len(auths) == 1:
            await self._run_auth(auths[0])
            return
        entries = [
            SelectorEntry(
                label=auth.label,
                value=auth,
                detail=self._auth_detail(auth),
                configured=auth.configured,
            )
            for auth in auths
        ]
        while True:
            chosen = await self.push_screen_wait(
                LoginScreen(f"Select authentication method for {name}:", entries)
            )
            if not isinstance(chosen, ProviderAuth):
                return
            if not await self._run_auth(chosen):
                return

    async def _login_interactive(self) -> None:
        auths = all_provider_auths(self.config)
        if not auths:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No providers are configured.", collapsed=self._tools_collapsed
                )
            )
            return

        kinds: set[AuthKind] = {auth.kind for auth in auths}
        kind: AuthKind
        if len(kinds) > 1:
            choices: list[SelectorEntry] = []
            if "oauth" in kinds:
                choices.append(
                    SelectorEntry("Sign in with a subscription or account", "oauth")
                )
            if "api_key" in kinds:
                choices.append(SelectorEntry("Sign in with an API key", "api_key"))
            if "ambient" in kinds:
                choices.append(
                    SelectorEntry("Configure ambient credentials", "ambient")
                )
            choice = await self.push_screen_wait(
                LoginScreen("Select authentication method:", choices)
            )
            if choice not in {"oauth", "api_key", "ambient"}:
                return
            kind = choice
        else:
            kind = next(iter(kinds))

        entries = [
            SelectorEntry(
                label=auth.provider,
                value=auth,
                detail=self._auth_detail(auth),
                configured=auth.configured,
            )
            for auth in auths
            if auth.kind == kind
        ]
        while True:
            chosen = await self.push_screen_wait(
                LoginScreen("Select provider:", entries)
            )
            if not isinstance(chosen, ProviderAuth):
                return
            if not await self._run_auth(chosen):
                return

    async def _run_auth(self, auth: ProviderAuth) -> bool:
        """Run one login method; return whether the user cancelled (go back)."""
        provider = find_provider(self.config, auth.provider)
        if provider is None:
            return False

        match auth.kind:
            case "oauth":
                result = await self._run_oauth_login(provider.name)
                if result.success:
                    await self._mount_and_scroll(
                        UserCommandMessage(
                            result.message or f"Signed in to {provider.name}."
                        )
                    )
                elif not result.cancelled and result.message:
                    await self._mount_and_scroll(
                        ErrorMessage(result.message, collapsed=self._tools_collapsed)
                    )
                return result.cancelled
            case "api_key":
                env_var = provider.api_key_env_var or "API key"
                secret = await self.push_screen_wait(
                    SecretScreen(f"Enter {env_var} for {provider.name}", env_var)
                )
                if not secret:
                    return True
                try:
                    path = save_api_key(provider, secret)
                except (OSError, ValueError) as exc:
                    await self._mount_and_scroll(
                        ErrorMessage(
                            f"Failed to save API key: {exc}",
                            collapsed=self._tools_collapsed,
                        )
                    )
                    return False
                await self._mount_and_scroll(
                    UserCommandMessage(
                        f"Saved {env_var} to {path}. New requests use it now."
                    )
                )
                return False
            case "ambient":
                await self._mount_and_scroll(
                    UserCommandMessage(
                        f"{provider.name} uses ambient credentials. "
                        f"{ambient_setup(provider.ambient)}"
                    )
                )
                return False

    async def _run_oauth_login(self, provider_name: str) -> LoginResult:
        try:
            flow = get_flow(provider_name)
        except UnknownOAuthFlowError as exc:
            return LoginResult(
                success=False, message=f"Login failed for {provider_name}: {exc}"
            )

        dialog = LoginDialogScreen(provider_name)
        await self.push_screen(dialog)
        try:
            token = await flow.login(dialog)
        except Exception as exc:
            if dialog.cancelled:
                return LoginResult(
                    success=False, message="Login cancelled", cancelled=True
                )
            result = LoginResult(
                success=False, message=f"Login failed for {provider_name}: {exc}"
            )
            await dialog.dismiss(result)
            return result

        if dialog.cancelled:
            return LoginResult(success=False, message="Login cancelled", cancelled=True)

        try:
            save_oauth_token(provider_name, token)
        except Exception as exc:
            result = LoginResult(
                success=False,
                message=f"Failed to save credentials for {provider_name}: {exc}",
            )
            await dialog.dismiss(result)
            return result

        result = LoginResult(success=True, message=f"Signed in to {provider_name}.")
        await dialog.dismiss(result)
        await self._reload_config()
        return result

    async def _logout_command(self, user_input: str = "") -> None:
        """``/logout [provider]``: remove stored credentials."""
        self.run_worker(self._logout_flow(user_input), exclusive=False, group="login")

    async def _logout_flow(self, user_input: str) -> None:
        name = self._command_argument(user_input)
        if name:
            notes = logout(self.config, name)
            await self._mount_and_scroll(
                UserCommandMessage(
                    "; ".join(notes)
                    if notes
                    else f"No stored credentials found for '{name}'."
                )
            )
            return

        auths = [
            auth
            for auth in all_provider_auths(self.config)
            if auth.configured and auth.kind != "ambient"
        ]
        if not auths:
            await self._mount_and_scroll(
                UserCommandMessage("No stored credentials to remove.")
            )
            return

        chosen = await self.push_screen_wait(
            LoginScreen(
                "Select credential to remove:",
                [
                    SelectorEntry(
                        label=auth.provider,
                        value=auth.provider,
                        detail=self._auth_detail(auth),
                    )
                    for auth in auths
                ],
            )
        )
        if not isinstance(chosen, str):
            return
        notes = logout(self.config, chosen)
        await self._mount_and_scroll(
            UserCommandMessage(
                "; ".join(notes) if notes else f"Nothing stored for '{chosen}'."
            )
        )

    def _register_dynamic_commands(self) -> None:
        for template in self.prompt_templates.values():
            if f"template:{template.name}" not in self.commands.commands:
                self.commands.register_template(template)

        for skill in self.skill_manager.get_available_skills():
            if f"skill:{skill.name}" not in self.commands.commands:
                self.commands.register_skill(skill)

        self._refresh_chat_completions()

    def _refresh_chat_completions(self) -> None:
        if self._chat_input_container is not None:
            self._chat_input_container.refresh_commands()

    async def _handle_command(self, user_input: str) -> bool:
        # Check if this is a command with arguments (e.g., /add-dir <path>)
        command_word = user_input.split()[0] if user_input else ""
        if command := self.commands.find_command(command_word):
            handler = getattr(self, command.handler)
            # Check if handler accepts user_input parameter
            import inspect

            sig = inspect.signature(handler)
            accepts_input = len(sig.parameters) > 0

            if asyncio.iscoroutinefunction(handler):
                if accepts_input:
                    await handler(user_input)
                else:
                    await handler()
            elif accepts_input:
                handler(user_input)
            else:
                handler()
            return True

        if command_word.startswith("/skill:"):
            await self._run_skill_command(user_input)
            return True
        return False

    async def _run_prompt_template(self, user_input: str) -> None:
        command_word = user_input.split(maxsplit=1)[0] if user_input else ""
        command = self.commands.find_command(command_word)
        if command is None or command.template is None:
            return

        template = self.prompt_templates.get(command.template)
        if template is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Prompt template '{command.template}' not found.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        raw_args = user_input[len(command_word) :].strip()
        await self._handle_user_message(render_template(template, raw_args).strip())

    async def _run_skill_command(self, user_input: str) -> None:
        command_word = user_input.split(maxsplit=1)[0] if user_input else ""
        command = self.commands.find_command(command_word)
        skill_name = (
            command.skill
            if command and command.skill
            else command_word.removeprefix("/skill:")
        )
        raw_args = user_input[len(command_word) :].strip()
        if not skill_name:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No skill name provided. Use: /skill:<name> [args]",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        try:
            message = self.skill_manager.render_skill_invocation(skill_name, raw_args)
        except (SkillNotFoundError, OSError) as exc:
            await self._mount_and_scroll(
                ErrorMessage(str(exc), collapsed=self._tools_collapsed)
            )
            return

        await self._handle_user_message(message)

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
            mcp_manager = MCPManager(self.config)
            await mcp_manager.start()
            self.mcp_manager = mcp_manager

            agent = Agent(
                self.config,
                mode=self.agent_mode,
                enable_streaming=self.enable_streaming,
                additional_directory_contexts=self._additional_directory_contexts,
                mcp_manager=mcp_manager,
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
                for message in non_system_messages:
                    agent.add_message(message)
                logger.info(
                    "Loaded %d messages from previous session", len(non_system_messages)
                )

            if self._initial_thinking_level is not None:
                agent.set_thinking_level(self._initial_thinking_level)
            if self._session_name:
                agent.record_custom("name", {"name": self._session_name})

            self.agent = agent
            self.skill_manager = agent.skill_manager
            self._register_dynamic_commands()
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
        self._pending_plan_approval: asyncio.Future[tuple[bool, str | None]] | None = (
            asyncio.Future()
        )
        self._pending_plan_content: str | None = plan
        await self._switch_to_plan_approval()
        result = await self._pending_plan_approval
        self._pending_plan_approval = None
        self._pending_plan_content = None
        return result

    async def _handle_agent_event(self, event: BaseEvent) -> None:
        if isinstance(event, ContextUsageEvent):
            if self._context_progress:
                self._context_progress.tokens = TokenState(
                    max_tokens=event.context_window, current_tokens=event.context_tokens
                )
                if self.agent:
                    self._context_progress.cost_usd = self.agent.stats.session_cost
            return

        if self._context_progress and self.agent:
            current_state = self._context_progress.tokens
            self._context_progress.tokens = TokenState(
                max_tokens=current_state.max_tokens,
                current_tokens=self.agent.stats.context_tokens,
            )
            self._context_progress.cost_usd = self.agent.stats.session_cost

        # Handle mode change events (legacy, for backward compatibility)
        if isinstance(event, ModeChangedEvent):
            self.agent_mode = event.new_mode
            # Use call_after_refresh to ensure ChatInputContainer children are composed
            self.call_after_refresh(self._sync_mode_to_ui)
            return

        if isinstance(event, SystemPromptChangedEvent):
            return

        if self.event_handler:
            await self.event_handler.handle_event(
                event,
                loading_active=self._loading_widget is not None,
                loading_widget=self._loading_widget,
            )

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
                await self._handle_agent_event(event)

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
        help_text = self.commands.get_help_text(self.keybinding_help_lines())
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

- **Model**: `{self.config.active_model}`
- **Turns**: {stats.steps:,}
- **Session Prompt Tokens**: {stats.session_prompt_tokens:,}
- **Session Completion Tokens**: {stats.session_completion_tokens:,}
- **Session Cache Read Tokens**: {stats.cache_read_tokens:,}
- **Session Cache Write Tokens**: {stats.cache_write_tokens:,}
- **Session Total LLM Tokens**: {stats.session_total_llm_tokens:,}
- **Last Turn Tokens**: {stats.last_turn_total_tokens:,}
- **Context**: {stats.context_percent:.0f}% ({stats.context_tokens:,}/{stats.context_window:,} tokens)
- **Cost**: ${stats.session_cost:.6f} (cache: ${stats.session_cache_cost:.6f})
- **Tools**: {stats.tool_calls_succeeded:,} ok / {stats.tool_calls_rejected:,} rejected / {stats.tool_calls_failed:,} failed
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
            self.skill_manager = SkillManager(new_config)
            self.prompt_templates = load_templates(
                self._default_template_dirs(self._prompt_template_dirs)
            )
            self._register_dynamic_commands()
            if self._context_progress:
                context_limit = self.config.get_context_limit_for_active_model()
                if context_limit > 0:
                    current_tokens = (
                        self.agent.stats.context_tokens if self.agent else 0
                    )
                    self._context_progress.tokens = TokenState(
                        max_tokens=context_limit, current_tokens=current_tokens
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
                UserCommandMessage("Context cleared. The session log is preserved.")
            )
            chat = self.query_one("#chat", VerticalScroll)
            chat.scroll_home(animate=False)

        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to clear history: {e}", collapsed=self._tools_collapsed
                )
            )

    async def _show_context(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No context yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        overview = context_overview(self.agent)
        await self._mount_and_scroll(UserCommandMessage(overview.describe()))

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

    def _update_context_progress(self) -> None:
        if not self._context_progress:
            return
        context_limit = self.config.get_context_limit_for_active_model()
        current_tokens = self.agent.stats.context_tokens if self.agent else 0
        if context_limit > 0:
            self._context_progress.tokens = TokenState(
                max_tokens=context_limit, current_tokens=current_tokens
            )
        else:
            self._context_progress.tokens = TokenState()
        self._context_progress.model = self.config.active_model
        self._context_progress.cost_usd = (
            self.agent.stats.session_cost if self.agent else 0.0
        )

    def _reasoning_capability(self) -> str:
        try:
            supported = self.config.get_active_model().capabilities.reasoning
        except ValueError:
            return ""
        if supported:
            return " This model supports reasoning."
        return " This model does not advertise reasoning support."

    async def _set_model_alias(self, alias: str, *, announce: bool = True) -> bool:
        if not self.config.is_model_in_scope(alias):
            if announce:
                await self._mount_and_scroll(
                    ErrorMessage(
                        f"Model '{alias}' is not in scope. "
                        "Use /scoped-models to change the scope.",
                        collapsed=self._tools_collapsed,
                    )
                )
            return False

        try:
            model = self.config.get_model_by_alias(alias)
        except ValueError as exc:
            if announce:
                await self._mount_and_scroll(
                    ErrorMessage(str(exc), collapsed=self._tools_collapsed)
                )
            return False

        if self.agent is not None:
            self.agent.set_model(alias)
        else:
            self.config.active_model = alias

        self._update_context_progress()
        if announce:
            await self._mount_and_scroll(
                UserCommandMessage(
                    f"Switched to model `{alias}` "
                    f"({model.provider}, {model.context_window:,} token context)."
                )
            )
        return True

    async def _show_model_selector(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) > 1 and (alias := parts[1].strip()):
            await self._set_model_alias(alias)
            return
        # ``push_screen_wait`` needs a worker, like the login flow.
        self.run_worker(
            self._open_model_selector(), exclusive=False, group="model-selector"
        )

    async def _open_model_selector(self) -> None:
        """Open the model picker listing the signed-in providers' models."""
        if self._current_bottom_app != BottomApp.Input:
            await self._switch_to_input_app()

        configured = configured_provider_names(self.config)
        models = [
            model
            for model in self.config.models_in_scope()
            if model.provider in configured
        ]
        hint = None
        if not models:
            hint = (
                "Only models from providers you are signed in to are listed. "
                "Run /login to add a provider."
            )
        chosen = await self.push_screen_wait(
            ModelSelectorScreen(models, self.config.active_model, hint=hint)
        )
        if isinstance(chosen, str) and chosen:
            await self._set_model_alias(chosen)

    async def _show_scoped_models(self) -> None:
        await self._switch_to_scoped_models_app()

    async def _cycle_model(self, direction: int) -> None:
        aliases = [model.alias for model in self.config.models_in_scope()]
        if not aliases:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No models are in scope. Use /scoped-models to enable some.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        current = self.config.active_model
        index = aliases.index(current) if current in aliases else -1
        target = aliases[(index + direction) % len(aliases)]
        await self._set_model_alias(target)

    async def _set_thinking_level(
        self, level: ThinkingLevel | None, *, announce: bool = True
    ) -> None:
        if self.agent is None:
            self._initial_thinking_level = level
        else:
            self.agent.set_thinking_level(level, record=True)

        if announce:
            label = level.value if level is not None else "default"
            await self._mount_and_scroll(
                UserCommandMessage(
                    f"Thinking level set to `{label}`.{self._reasoning_capability()}"
                )
            )

    async def _set_thinking(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            current = (
                self.agent.thinking_level
                if self.agent
                else self._initial_thinking_level
            )
            label = current.value if current is not None else "default"
            valid = ", ".join(level.value for level in THINKING_LEVELS)
            await self._mount_and_scroll(
                UserCommandMessage(
                    f"Thinking level: `{label}`.{self._reasoning_capability()}\n\n"
                    f"Valid levels: {valid}, or `default`."
                )
            )
            return

        raw_level = parts[1].strip().lower()
        match raw_level:
            case "default" | "none" | "auto" | "":
                level: ThinkingLevel | None = None
            case "off":
                level = ThinkingLevel.OFF
            case raw:
                try:
                    level = ThinkingLevel(raw)
                except ValueError:
                    valid = ", ".join(entry.value for entry in THINKING_LEVELS)
                    await self._mount_and_scroll(
                        ErrorMessage(
                            f"Invalid thinking level '{raw}'. "
                            f"Valid levels: {valid}, or default.",
                            collapsed=self._tools_collapsed,
                        )
                    )
                    return
        await self._set_thinking_level(level)

    async def _cycle_thinking(self) -> None:
        current = (
            self.agent.thinking_level if self.agent else self._initial_thinking_level
        )
        try:
            levels = list(supported_thinking_levels(self.config.get_active_model()))
        except ValueError:
            levels = list(THINKING_LEVELS)
        current_index = next(
            (index for index, level in enumerate(levels) if level == current), -1
        )
        target = levels[(current_index + 1) % len(levels)]
        await self._set_thinking_level(target)

    async def _new_session(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        await self.agent.switch_session(
            session_id=str(uuid4()), filepath=None, messages=[], stats=AgentStats()
        )
        await self._finalize_current_streaming_message()
        messages_area = self.query_one("#messages")
        await messages_area.remove_children()
        self._session_name = None
        self._update_context_progress()
        await self._mount_and_scroll(
            UserCommandMessage(f"Started a new session `{self.agent.session_id[:8]}`.")
        )

    async def _resume_session(self, user_input: str) -> None:
        if not self.config.session_logging.enabled:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Session logging is disabled. Enable it in config to resume.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        parts = user_input.split(maxsplit=1)
        if len(parts) > 1 and (session_id := parts[1].strip()):
            filepath = InteractionLogger.find_session_by_id(
                session_id, self.config.session_logging
            )
            if filepath is None:
                await self._mount_and_scroll(
                    ErrorMessage(
                        f"Session '{session_id}' not found.",
                        collapsed=self._tools_collapsed,
                    )
                )
                return
            await self._load_session_file(filepath)
            return

        await self._switch_to_conversations_app()

    async def _set_session_name(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            current = self._session_name or "(unnamed)"
            await self._mount_and_scroll(
                UserCommandMessage(
                    f"Session name: {current}\n\nUse `/name <name>` to rename."
                )
            )
            return

        self._session_name = name
        if self.agent is not None:
            self.agent.record_custom("name", {"name": name})
        await self._mount_and_scroll(UserCommandMessage(f"Session named `{name}`."))

    async def _show_session_info(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        stats = self.agent.stats
        session_file: str | None = None
        entry_count: int | None = None
        filepath = self.agent.interaction_logger.filepath
        if self.config.session_logging.enabled and filepath is not None:
            session_file = str(filepath)
            if filepath.suffix == ".jsonl":
                try:
                    view = JsonlSessionStore(self.config.session_logging).read(filepath)
                    entry_count = len(view.entries) + 1
                except OSError:
                    entry_count = None

        lines = [
            "## Session Info",
            "",
            f"- **ID**: `{self.agent.session_id[:8]}`",
            f"- **Name**: {self._session_name or '(unnamed)'}",
            f"- **Model**: `{self.config.active_model}`",
            f"- **Context**: {stats.context_percent:.0f}% "
            f"({stats.context_tokens:,}/{stats.context_window:,} tokens)",
            f"- **Cost**: ${stats.session_cost:.6f}",
            f"- **Messages**: {max(0, len(self.agent.messages) - 1)}",
            f"- **Entries**: {entry_count if entry_count is not None else 'n/a'}",
            f"- **Session file**: `{session_file or 'disabled'}`",
        ]
        await self._mount_and_scroll(UserCommandMessage("\n".join(lines)))

    async def _export_session(self, user_input: str) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        parts = user_input.split(maxsplit=1)
        workdir = Path(self.config.effective_workdir)
        if len(parts) > 1 and (raw_path := parts[1].strip()):
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                target = workdir / target
        else:
            target = workdir / f"session_{self.agent.session_id[:8]}.md"

        metadata = {
            "session_id": self.agent.session_id,
            "model": self.config.active_model,
        }
        try:
            export_session(
                target,
                source=self.agent.interaction_logger.filepath,
                messages=self.agent.messages,
                metadata=metadata,
            )
        except OSError as exc:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to export session: {exc}", collapsed=self._tools_collapsed
                )
            )
            return

        await self._mount_and_scroll(
            UserCommandMessage(f"Exported session to `{target}`.")
        )

    async def _import_session(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1 or not (raw_path := parts[1].strip()):
            await self._mount_and_scroll(
                ErrorMessage(
                    "Usage: /import <path-to-.jsonl-or-.json>",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        source = Path(raw_path).expanduser()
        try:
            imported = import_session(
                source,
                store=JsonlSessionStore(self.config.session_logging),
                workdir=Path(self.config.effective_workdir),
                persist=self.config.session_logging.enabled,
            )
        except (ValueError, OSError) as exc:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to import session: {exc}", collapsed=self._tools_collapsed
                )
            )
            return

        await self.agent.switch_session(
            session_id=imported.session_id,
            filepath=imported.path,
            messages=imported.messages,
            stats=imported.stats,
        )
        await self._finalize_current_streaming_message()
        await self._reset_ui_messages()
        await self._restore_ui_from_messages(imported.messages)
        await self._mount_and_scroll(
            UserCommandMessage(
                f"Imported session `{imported.session_id[:8]}` "
                f"({len(imported.messages)} messages)."
            )
        )

    async def _reset_ui_messages(self) -> None:
        messages_area = self.query_one("#messages")
        await messages_area.remove_children()
        self._update_context_progress()
        chat = self.query_one("#chat", VerticalScroll)
        chat.scroll_home(animate=False)

    async def _fork_session(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not self.config.session_logging.enabled:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Session logging is disabled. Enable it in config to fork.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        filepath = self.agent.interaction_logger.filepath
        if filepath is None or filepath.suffix != ".jsonl":
            await self._mount_and_scroll(
                ErrorMessage(
                    "No JSONL session to fork yet.", collapsed=self._tools_collapsed
                )
            )
            return

        try:
            view = JsonlSessionStore(self.config.session_logging).read(filepath)
        except OSError as exc:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to read session: {exc}", collapsed=self._tools_collapsed
                )
            )
            return

        candidates = [
            (entry.id, entry.message.content or "")
            for entry in view.entries
            if isinstance(entry, MessageEntry)
            and entry.message.role is Role.user
            and (entry.message.content or "").strip()
        ][-20:]
        if not candidates:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No user messages to fork from.", collapsed=self._tools_collapsed
                )
            )
            return

        await self._switch_to_fork_app(candidates)

    async def _copy_last_message(self) -> None:
        if self.agent is None:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        content = next(
            (
                message.content
                for message in reversed(self.agent.messages)
                if message.role is Role.assistant and (message.content or "").strip()
            ),
            None,
        )
        if not content:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No assistant message to copy yet.", collapsed=self._tools_collapsed
                )
            )
            return

        copied = copy_text_to_clipboard(
            self, content, f"Copied last assistant message ({len(content)} chars)."
        )
        if not copied:
            await self._mount_and_scroll(
                ErrorMessage("Clipboard unavailable.", collapsed=self._tools_collapsed)
            )

    async def _show_conversations(self) -> None:
        """Show the conversations browser to select a past session."""
        if not self.config.session_logging.enabled:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Session logging is disabled. Enable it in config to browse past conversations.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if self._current_bottom_app == BottomApp.Conversations:
            return

        await self._switch_to_conversations_app()

    async def _compact_history(self, user_input: str) -> None:
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

        parts = user_input.split(maxsplit=1)
        instructions = parts[1].strip() if len(parts) > 1 else None

        try:
            await self.agent.compact(instructions=instructions)
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

    async def _add_directory_context(self, user_input: str) -> None:
        """Add directory context to the conversation."""
        if not self.agent:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Agent not initialized yet. Send a message first.",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        # Extract directory path from command
        command_text = user_input.strip()
        if command_text.startswith("/add-dir "):
            dir_path_str = command_text[len("/add-dir ") :].strip()
        elif command_text.startswith("/add-directory "):
            dir_path_str = command_text[len("/add-directory ") :].strip()
        else:
            await self._mount_and_scroll(
                ErrorMessage(
                    "Invalid command format. Use: /add-dir <directory_path>",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not dir_path_str:
            await self._mount_and_scroll(
                ErrorMessage(
                    "No directory path provided. Use: /add-dir <directory_path>",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        # Convert to Path object and resolve
        dir_path = Path(dir_path_str).expanduser().resolve()

        # Validate directory exists
        if not dir_path.exists():
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Directory does not exist: {dir_path}",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        if not dir_path.is_dir():
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Path is not a directory: {dir_path}",
                    collapsed=self._tools_collapsed,
                )
            )
            return

        try:
            # Add the directory to the agent's additional contexts
            self.agent.add_additional_directory_context(dir_path)

            # Regenerate the system prompt with the new context
            self.agent._regenerate_system_prompt_with_additional_contexts()

            await self._mount_and_scroll(
                UserCommandMessage(f"Added directory context: {dir_path}")
            )
        except Exception as e:
            await self._mount_and_scroll(
                ErrorMessage(
                    f"Failed to add directory context: {e}",
                    collapsed=self._tools_collapsed,
                )
            )

    async def _switch_to_config_app(self, focus_key: str | None = None) -> None:
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

        config_app = ConfigApp(self.config, focus_key=focus_key)
        await bottom_container.mount(config_app)
        self._current_bottom_app = BottomApp.Config

        self.call_after_refresh(config_app.focus)

    async def _switch_to_scoped_models_app(self) -> None:
        if self._current_bottom_app == BottomApp.ScopedModels:
            return

        bottom_container = self.query_one("#bottom-app-container")

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            await chat_input_container.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = False

        scoped_app = ScopedModelsApp(self.config)
        await bottom_container.mount(scoped_app)
        self._current_bottom_app = BottomApp.ScopedModels

        self.call_after_refresh(scoped_app.focus)

    async def _switch_to_fork_app(self, candidates: list[tuple[str, str]]) -> None:
        if self._current_bottom_app == BottomApp.Fork:
            return

        bottom_container = self.query_one("#bottom-app-container")

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            await chat_input_container.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = False

        fork_app = ForkApp(candidates)
        await bottom_container.mount(fork_app)
        self._current_bottom_app = BottomApp.Fork

        self.call_after_refresh(fork_app.focus)

    async def _switch_to_conversations_app(self) -> None:
        if self._current_bottom_app == BottomApp.Conversations:
            return

        bottom_container = self.query_one("#bottom-app-container")

        try:
            chat_input_container = self.query_one(ChatInputContainer)
            await chat_input_container.remove()
        except Exception:
            pass

        if self._mode_indicator:
            self._mode_indicator.display = False

        # Load sessions list for current project only
        sessions = InteractionLogger.list_sessions(
            self.config.session_logging, workdir=self.config.effective_workdir
        )

        conversations_app = ConversationsApp(sessions)
        await bottom_container.mount(conversations_app)
        self._current_bottom_app = BottomApp.Conversations

        self.call_after_refresh(conversations_app.focus)

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
            conversations_app = self.query_one("#conversations-app")
            await conversations_app.remove()
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

        try:
            scoped_app = self.query_one("#scoped-models-app")
            await scoped_app.remove()
        except Exception:
            pass

        try:
            fork_app = self.query_one("#fork-app")
            await fork_app.remove()
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
                case BottomApp.Conversations:
                    self.query_one(ConversationsApp).focus()
                case BottomApp.Approval:
                    self.query_one(ApprovalApp).focus()
                case BottomApp.PlanApproval:
                    self.query_one(PlanApprovalWidget).focus()
                case BottomApp.ScopedModels:
                    self.query_one(ScopedModelsApp).focus()
                case BottomApp.Fork:
                    self.query_one(ForkApp).focus()
                case app:
                    assert_never(app)
        except Exception:
            pass

    def action_interrupt(self) -> None:
        if isinstance(self.screen, ModalScreen):
            raise SkipAction

        if self._current_bottom_app == BottomApp.Config:
            try:
                config_app = self.query_one(ConfigApp)
                config_app.action_close()
            except Exception:
                pass
            return

        if self._current_bottom_app == BottomApp.Conversations:
            try:
                conversations_app = self.query_one(ConversationsApp)
                conversations_app.action_close()
            except Exception:
                pass
            return

        if self._current_bottom_app == BottomApp.ScopedModels:
            try:
                scoped_app = self.query_one(ScopedModelsApp)
                scoped_app.action_close()
            except Exception:
                pass
            return

        if self._current_bottom_app == BottomApp.Fork:
            try:
                fork_app = self.query_one(ForkApp)
                fork_app.action_close()
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

    async def action_cycle_model(self) -> None:
        if self._current_bottom_app != BottomApp.Input:
            return
        await self._cycle_model(1)

    async def action_cycle_model_previous(self) -> None:
        if self._current_bottom_app != BottomApp.Input:
            return
        await self._cycle_model(-1)

    async def action_cycle_thinking(self) -> None:
        if self._current_bottom_app != BottomApp.Input:
            return
        await self._cycle_thinking()

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
    additional_directory_contexts: list[Path] | None = None,
    prompt_template_dirs: list[Path] | None = None,
    resume: UISessionOptions | None = None,
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
        additional_directory_contexts=additional_directory_contexts,
        prompt_template_dirs=prompt_template_dirs,
        resume=resume,
    )
    app.run()
