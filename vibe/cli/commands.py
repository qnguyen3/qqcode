from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import TYPE_CHECKING

from vibe.cli.keybindings import DEFAULT_HELP_LINES as DEFAULT_KEYBINDING_HELP_LINES

if TYPE_CHECKING:
    from vibe.core.prompts.templates import PromptTemplate
    from vibe.core.skills.manager import SkillInfo

logger = getLogger("vibe")


@dataclass
class Command:
    aliases: frozenset[str]
    description: str
    handler: str
    exits: bool = False
    template: str | None = None
    skill: str | None = None


class CommandRegistry:
    def __init__(
        self,
        excluded_commands: list[str] | None = None,
        dynamic_commands: list[Command] | None = None,
    ) -> None:
        if excluded_commands is None:
            excluded_commands = []
        self.commands = {
            "help": Command(
                aliases=frozenset(["/help", "/h"]),
                description="Show help message",
                handler="_show_help",
            ),
            "status": Command(
                aliases=frozenset(["/status", "/stats"]),
                description="Display agent statistics",
                handler="_show_status",
            ),
            "config": Command(
                aliases=frozenset(["/config", "/cfg", "/theme"]),
                description="Edit config settings",
                handler="_show_config",
            ),
            "model": Command(
                aliases=frozenset(["/model"]),
                description="Switch model or open the model selector (Ctrl+P cycles)",
                handler="_show_model_selector",
            ),
            "scoped_models": Command(
                aliases=frozenset(["/scoped-models", "/models"]),
                description="Choose which models are selectable in this scope",
                handler="_show_scoped_models",
            ),
            "thinking": Command(
                aliases=frozenset(["/thinking"]),
                description="Set thinking level, or show the current one (Ctrl+Y cycles)",
                handler="_set_thinking",
            ),
            "new": Command(
                aliases=frozenset(["/new"]),
                description="Start a new session, keeping the current config",
                handler="_new_session",
            ),
            "resume": Command(
                aliases=frozenset(["/resume"]),
                description="Resume a session: pick one, or pass an id",
                handler="_resume_session",
            ),
            "name": Command(
                aliases=frozenset(["/name"]),
                description="Set or show the session name",
                handler="_set_session_name",
            ),
            "session": Command(
                aliases=frozenset(["/session"]),
                description="Show current session info (id, model, context, cost)",
                handler="_show_session_info",
            ),
            "export": Command(
                aliases=frozenset(["/export"]),
                description="Export the session as markdown, .jsonl or .html",
                handler="_export_session",
            ),
            "import": Command(
                aliases=frozenset(["/import"]),
                description="Replace the current session with a .jsonl/.json file",
                handler="_import_session",
            ),
            "fork": Command(
                aliases=frozenset(["/fork"]),
                description="Fork the session from a recent user message",
                handler="_fork_session",
            ),
            "copy": Command(
                aliases=frozenset(["/copy"]),
                description="Copy the last assistant message to the clipboard",
                handler="_copy_last_message",
            ),
            "reload": Command(
                aliases=frozenset(["/reload", "/r"]),
                description="Reload configuration from disk",
                handler="_reload_config",
            ),
            "clear": Command(
                aliases=frozenset(["/clear", "/reset"]),
                description="Clear the context (the session log is kept)",
                handler="_clear_history",
            ),
            "context": Command(
                aliases=frozenset(["/context"]),
                description="Show what the model sees: head marker, edits, recent entries",
                handler="_show_context",
            ),
            "log": Command(
                aliases=frozenset(["/log", "/logpath"]),
                description="Show path to current interaction log file",
                handler="_show_log_path",
            ),
            "conversations": Command(
                aliases=frozenset(["/conversations", "/sessions", "/history"]),
                description="Browse and continue past conversations",
                handler="_show_conversations",
            ),
            "compact": Command(
                aliases=frozenset(["/compact", "/summarize"]),
                description="Compact conversation history by summarizing (optional instructions)",
                handler="_compact_history",
            ),
            "exit": Command(
                aliases=frozenset(["/exit", "/quit", "/q"]),
                description="Exit the application",
                handler="_exit_app",
                exits=True,
            ),
            "add_dir": Command(
                aliases=frozenset(["/add-dir", "/add-directory"]),
                description="Add directory context to the conversation",
                handler="_add_directory_context",
            ),
            "hotkeys": Command(
                aliases=frozenset(["/hotkeys", "/keys"]),
                description="Show keyboard shortcuts (use /hotkeys edit to customize)",
                handler="_show_hotkeys",
            ),
            "login": Command(
                aliases=frozenset(["/login", "/signin"]),
                description="Sign in to a provider or store an API key",
                handler="_login_command",
            ),
            "logout": Command(
                aliases=frozenset(["/logout"]),
                description="Remove a stored provider credential",
                handler="_logout_command",
            ),
        }

        for command in excluded_commands:
            self.commands.pop(command, None)

        for index, command in enumerate(dynamic_commands or []):
            match command:
                case Command(template=template) if template:
                    key = f"template:{template}"
                case Command(skill=skill) if skill:
                    key = f"skill:{skill}"
                case _:
                    key = f"dynamic:{index}"
            self.register_command(key, command)

        self._alias_map = {}
        for cmd_name, cmd in self.commands.items():
            for alias in cmd.aliases:
                self._alias_map[alias] = cmd_name

    def register_command(self, key: str, command: Command) -> bool:
        """Register a dynamic command, rejecting duplicate names or aliases."""
        if key in self.commands:
            logger.warning("Command '%s' is already registered; ignoring", key)
            return False

        for alias in command.aliases:
            if alias in self._alias_map:
                logger.warning(
                    "Command alias '%s' is already registered; ignoring '%s'",
                    alias,
                    key,
                )
                return False

        self.commands[key] = command
        for alias in command.aliases:
            self._alias_map[alias] = key
        return True

    def register_template(self, template: PromptTemplate) -> bool:
        """Register a prompt template as ``/<name>``; built-ins win collisions."""
        return self.register_command(
            f"template:{template.name}",
            Command(
                aliases=frozenset([f"/{template.name}"]),
                description=template.description
                or f"Run prompt template '{template.name}'",
                handler="_run_prompt_template",
                template=template.name,
            ),
        )

    def register_skill(self, skill: SkillInfo) -> bool:
        """Register a skill as ``/skill:<name>``."""
        return self.register_command(
            f"skill:{skill.name}",
            Command(
                aliases=frozenset([f"/skill:{skill.name}"]),
                description=skill.description or f"Invoke skill '{skill.name}'",
                handler="_run_skill_command",
                skill=skill.name,
            ),
        )

    def find_command(self, user_input: str) -> Command | None:
        cmd_name = self._alias_map.get(user_input.lower().strip())
        return self.commands.get(cmd_name) if cmd_name else None

    def command_entries(self) -> list[tuple[str, str]]:
        """(alias, description) pairs for autocompletion, sorted by alias."""
        return [
            (alias, command.description)
            for command in self.commands.values()
            for alias in sorted(command.aliases)
        ]

    def get_help_text(self, keybinding_lines: list[str] | None = None) -> str:
        lines: list[str] = [
            "### Keyboard Shortcuts",
            "",
            "- `Enter` Submit message",
            "- `Ctrl+J` / `Shift+Enter` Insert newline",
            *(
                keybinding_lines
                if keybinding_lines is not None
                else DEFAULT_KEYBINDING_HELP_LINES
            ),
            "",
            "### Special Features",
            "",
            "- `!<command>` Execute bash command directly",
            "- `@path/to/file/` Autocompletes file paths",
            "",
            "### Commands",
            "",
        ]

        for cmd in self.commands.values():
            aliases = ", ".join(f"`{alias}`" for alias in sorted(cmd.aliases))
            lines.append(f"- {aliases}: {cmd.description}")
        return "\n".join(lines)
