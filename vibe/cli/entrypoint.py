from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from rich import print as rprint
from rich.table import Table

from vibe.cli.login import run_login_command, run_logout_command
from vibe.cli.mcp_cli import run_mcp_command
from vibe.cli.textual_ui.app import UISessionOptions, run_textual_ui
from vibe.core.config import (
    CONFIG_FILE,
    HISTORY_FILE,
    INSTRUCTIONS_FILE,
    MissingAPIKeyError,
    MissingPromptFileError,
    ModelConfig,
    SessionLoggingConfig,
    ThinkingLevel,
    VibeConfig,
    load_api_keys_from_env,
)
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.model_catalog import load_model_snapshot, snapshot_problems
from vibe.core.models_overlay import validate_models
from vibe.core.packages import (
    InstalledPackage,
    PackageError,
    install,
    list_packages,
    remove,
    update,
)
from vibe.core.programmatic import run_programmatic
from vibe.core.sessions.migrate import migrate_directory
from vibe.core.sessions.store import JsonlSessionStore
from vibe.core.trust import (
    ProjectResources,
    TrustPrompt,
    TrustStore,
    pending_trust,
    read_default_project_trust,
)
from vibe.core.types import LLMMessage, OutputFormat, ResumeSessionInfo
from vibe.core.utils import ConversationLimitException
from vibe.setup.onboarding import run_onboarding


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the QQcode interactive CLI",
        epilog=(
            "Use 'qqcode login [provider]' to store API keys or sign in to a "
            "subscription, 'qqcode sessions [list|repair|migrate]' to manage "
            "saved sessions, 'qqcode models [list|validate]' to inspect the "
            "model catalog, or 'qqcode mcp [list|add|remove|login|logout]' to "
            "manage MCP servers."
        ),
    )
    parser.add_argument(
        "initial_prompt",
        nargs="?",
        metavar="PROMPT",
        help="Initial prompt to start the interactive session with.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        nargs="?",
        const="",
        metavar="TEXT",
        help="Run in programmatic mode: send prompt (read-only by default), "
        "output response, and exit. Use --auto-approve to allow tool execution.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Automatically approve all tool executions (shortcut for --mode auto-approve).",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["plan", "interactive", "auto-approve"],
        default=None,
        help="Execution mode for programmatic mode (-p): "
        "'plan' (read-only, default), 'interactive' (approval required via stdin), "
        "'auto-approve' (execute all tools automatically).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="Maximum number of assistant turns "
        "(only applies in programmatic mode with -p).",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        metavar="DOLLARS",
        help="Maximum cost in dollars (only applies in programmatic mode with -p). "
        "Session will be interrupted if cost exceeds this limit.",
    )
    parser.add_argument(
        "--tools",
        "--enabled-tools",
        dest="tools",
        action="append",
        metavar="TOOL",
        help="Enable specific tools (alias: --enabled-tools). In programmatic "
        "mode (-p), this disables all other tools. Supports '+name' to include, "
        "'-name' to exclude, exact names, glob patterns (e.g., 'bash*'), or "
        "regex with 're:' prefix. Can be specified multiple times.",
    )
    parser.add_argument(
        "--exclude-tools",
        action="append",
        metavar="TOOL",
        help="Disable specific tools (supports '+name'/'-name' and glob patterns). "
        "Ignored when --tools selects an explicit allowlist. Can be specified "
        "multiple times.",
    )
    parser.add_argument(
        "--system-prompt",
        metavar="TEXT|FILE",
        help="Replace the system prompt. The value is read from a file when it "
        "points to an existing path, otherwise used as literal text.",
    )
    parser.add_argument(
        "--append-system-prompt",
        action="append",
        metavar="TEXT|FILE",
        help="Append text to the system prompt (repeatable; file paths are read).",
    )
    parser.add_argument(
        "--no-context-files",
        action="store_true",
        default=False,
        help="Do not load context files (AGENTS.md, QQCODE.md) into the prompt.",
    )
    parser.add_argument(
        "--session-dir", metavar="DIR", help="Override the session storage directory."
    )
    parser.add_argument(
        "--name", metavar="NAME", help="Give the session a human readable name."
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        default=False,
        help="Disable session logging for this run.",
    )
    parser.add_argument(
        "--fork",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help="Fork the latest session (or SESSION_ID) and open the copy.",
    )
    parser.add_argument(
        "--prompt-template",
        action="append",
        metavar="DIR",
        help="Additional prompt template directory (repeatable).",
    )
    parser.add_argument(
        "--output",
        type=str,
        choices=["text", "json", "streaming", "vscode", "events"],
        default="text",
        help="Output format for programmatic mode (-p): 'text' "
        "for human-readable (default), 'json' for all messages at end, "
        "'streaming' for newline-delimited JSON per message, "
        "'vscode' for structured streaming events (VSCode extension), "
        "'events' for the versioned JSONL event stream (see docs/cli-events.md).",
    )
    parser.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="Load agent configuration from ~/.qqcode/agents/NAME.toml",
    )
    parser.add_argument("--setup", action="store_true", help="Setup API key and exit")
    parser.add_argument(
        "--login",
        metavar="PROVIDER",
        choices=["anthropic", "qwen"],
        help="Login to a provider using OAuth (e.g., --login anthropic, --login qwen)",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available sessions and exit (outputs JSON)",
    )
    parser.add_argument(
        "--get-session",
        type=str,
        metavar="SESSION_ID",
        help="Load session data and output as JSON (for UI population)",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available models and exit (outputs JSON)",
    )
    parser.add_argument(
        "--get-model",
        action="store_true",
        help="Get current active model and exit (outputs JSON)",
    )
    parser.add_argument(
        "--model",
        type=str,
        metavar="ALIAS",
        help="Use specified model for this session (temporary override)",
    )

    continuation_group = parser.add_mutually_exclusive_group()
    continuation_group.add_argument(
        "-c",
        "--continue",
        action="store_true",
        dest="continue_session",
        help="Continue from the most recent saved session",
    )
    continuation_group.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a specific session by its ID (supports partial matching)",
    )
    trust_group = parser.add_mutually_exclusive_group()
    trust_group.add_argument(
        "--trust",
        action="store_true",
        dest="trust",
        default=None,
        help="Trust the current project and load its .qqcode resources.",
    )
    trust_group.add_argument(
        "--no-trust",
        action="store_false",
        dest="trust",
        help="Do not load project resources (headless/CI safe).",
    )
    return parser.parse_args()


def get_prompt_from_stdin() -> str | None:
    if sys.stdin.isatty():
        return None
    try:
        if content := sys.stdin.read().strip():
            sys.stdin = sys.__stdin__ = open("/dev/tty")
            return content
    except KeyboardInterrupt:
        pass
    except OSError:
        return None

    return None


def _resolve_prompt_value(value: str) -> str:
    """Read prompt text from an existing file path, else use the literal value."""
    path = Path(value).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _parse_tool_flags(values: list[str], *, bare: str) -> tuple[list[str], list[str]]:
    """Split ``+name``/``-name`` tool flags into (enabled, disabled) patterns.

    Bare values go to the bucket named by ``bare`` (``"enabled"`` or
    ``"disabled"``).
    """
    enabled: list[str] = []
    disabled: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        match value[:1]:
            case "+" if len(value) > 1:
                enabled.append(value[1:])
            case "-" if len(value) > 1:
                disabled.append(value[1:])
            case _:
                target = enabled if bare == "enabled" else disabled
                target.append(value)
    return enabled, disabled


def apply_runtime_flags(config: VibeConfig, args: argparse.Namespace) -> list[Path]:
    """Apply runtime CLI flags to ``config``.

    Returns the prompt template directories requested on the command line.
    """
    if args.system_prompt:
        config.system_prompt_override = _resolve_prompt_value(args.system_prompt)
    if args.append_system_prompt:
        config.append_system_prompt = [
            _resolve_prompt_value(value) for value in args.append_system_prompt
        ]
    if args.no_context_files:
        config.include_context_files = False
    if args.session_dir:
        session_dir = str(Path(args.session_dir).expanduser().resolve())
        config.session_logging.sessions_dir = session_dir
        config.session_logging.save_dir = session_dir
    if args.no_session:
        config.session_logging.enabled = False

    enabled, disabled = _parse_tool_flags(args.tools or [], bare="enabled")
    extra_enabled, extra_disabled = _parse_tool_flags(
        args.exclude_tools or [], bare="disabled"
    )
    enabled.extend(extra_enabled)
    disabled.extend(extra_disabled)
    if enabled:
        config.enabled_tools = enabled
    if disabled:
        config.disabled_tools = disabled

    return [Path(directory).expanduser() for directory in args.prompt_template or []]


def _restore_session_preferences(
    config: VibeConfig, metadata: dict[str, Any]
) -> tuple[ThinkingLevel | None, str | None]:
    """Restore model, thinking level and name persisted in a session."""
    custom = metadata.get("custom")
    if not isinstance(custom, dict):
        custom = {}

    model_value = custom.get("model")
    if isinstance(model_value, dict) and isinstance(model_value.get("alias"), str):
        alias = model_value["alias"]
        if any(model.alias == alias for model in config.models):
            config.active_model = alias

    raw_level: str | None = None
    level_value = custom.get("thinking_level")
    if isinstance(level_value, dict) and isinstance(level_value.get("level"), str):
        raw_level = level_value["level"]
    elif isinstance(metadata.get("thinking_level"), str):
        raw_level = metadata["thinking_level"]

    level: ThinkingLevel | None = None
    if raw_level is not None:
        try:
            level = ThinkingLevel(raw_level)
        except ValueError:
            level = None

    name_value = custom.get("name")
    name = name_value.get("name") if isinstance(name_value, dict) else None
    return level, name if isinstance(name, str) else None


def load_config_or_exit(
    agent: str | None = None,
    *,
    trust: bool | None = None,
    trust_callback: TrustPrompt | None = None,
) -> VibeConfig:
    try:
        config, resolution = VibeConfig.load_with_trust(
            agent, trust=trust, trust_callback=trust_callback
        )
        if resolution.project is not None and not resolution.trusted:
            rprint(
                f"[yellow]Skipping project resources in "
                f"{resolution.project.project_dir} ({', '.join(resolution.project.resource_labels)}): "
                "project not trusted. Use --trust to allow them.[/]"
            )
        return config
    except MissingAPIKeyError:
        run_onboarding()
        return VibeConfig.load(agent, trust=trust)
    except MissingPromptFileError as e:
        rprint(f"[yellow]Invalid system prompt id: {e}[/]")
        sys.exit(1)
    except ValueError as e:
        rprint(f"[yellow]{e}[/]")
        sys.exit(1)


def _build_sessions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqcode sessions", description="Manage saved QQcode sessions"
    )
    subparsers = parser.add_subparsers(dest="sessions_command", required=True)
    subparsers.add_parser("list", help="List recent sessions (outputs JSON)")
    stats = subparsers.add_parser(
        "stats", help="Aggregate usage/cost for a session (outputs JSON)"
    )
    stats.add_argument("session_id", metavar="SESSION_ID")
    repair = subparsers.add_parser(
        "repair", help="Truncate a damaged session file at its last valid line"
    )
    repair.add_argument("session_id", metavar="SESSION_ID")
    migrate = subparsers.add_parser(
        "migrate", help="Convert legacy .json sessions to .jsonl"
    )
    migrate.add_argument(
        "--dir",
        dest="directory",
        metavar="DIR",
        default=None,
        help="Directory containing legacy .json sessions (defaults to save_dir)",
    )
    return parser


def _session_error(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return 1


def _run_session_stats(
    session_id: str, session_config: SessionLoggingConfig, store: JsonlSessionStore
) -> int:
    path = InteractionLogger.find_session_by_id(session_id, session_config)
    if path is None:
        return _session_error(f"Session '{session_id}' not found")
    if path.suffix != ".jsonl":
        return _session_error("Only .jsonl sessions support stats")
    print(json.dumps(store.read(path).usage_stats().model_dump()))
    return 0


def _run_session_repair(
    session_id: str, session_config: SessionLoggingConfig, store: JsonlSessionStore
) -> int:
    path = InteractionLogger.find_session_by_id(session_id, session_config)
    if path is None:
        return _session_error(f"Session '{session_id}' not found")
    if path.suffix != ".jsonl":
        return _session_error("Only .jsonl sessions can be repaired")
    result = store.repair(path)
    print(
        json.dumps({
            "path": str(result.path),
            "kept_entries": result.kept_entries,
            "removed_bytes": result.removed_bytes,
            "backup_path": (str(result.backup_path) if result.backup_path else None),
        })
    )
    return 0


def run_sessions_command(argv: list[str]) -> int:
    args = _build_sessions_parser().parse_args(argv)
    config = load_config_or_exit()
    session_config = config.session_logging
    store = JsonlSessionStore(session_config)

    match args.sessions_command:
        case "list":
            sessions = InteractionLogger.list_sessions(session_config, limit=50)
            print(
                json.dumps([
                    {
                        "session_id": summary.get("session_id", ""),
                        "end_time": summary.get("end_time", ""),
                        "last_user_message": summary.get("last_user_message", ""),
                    }
                    for _, summary in sessions
                ])
            )
            return 0
        case "stats":
            return _run_session_stats(args.session_id, session_config, store)
        case "repair":
            return _run_session_repair(args.session_id, session_config, store)
        case "migrate":
            directory = (
                Path(args.directory)
                if args.directory
                else Path(session_config.save_dir)
            )
            results = migrate_directory(directory, store)
            print(
                json.dumps([
                    {
                        "source": str(result.source),
                        "target": str(result.target) if result.target else None,
                        "backup": str(result.backup) if result.backup else None,
                        "error": result.error,
                    }
                    for result in results
                ])
            )
            return 0

    return 1


def _build_models_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqcode models", description="Inspect the model catalog"
    )
    subparsers = parser.add_subparsers(dest="models_command", required=True)
    list_parser = subparsers.add_parser("list", help="List available models")
    list_parser.add_argument(
        "--json", action="store_true", help="Output the catalog as JSON"
    )
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate model aliases, providers and the generated model snapshot",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a snapshot older than 30 days as an error",
    )
    return parser


def _model_catalog_entry(model: ModelConfig) -> dict[str, Any]:
    return {
        "alias": model.alias,
        "name": model.name,
        "provider": model.provider,
        "context_limit": model.context_window,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "input_price": model.pricing.input_per_million,
        "output_price": model.pricing.output_per_million,
        "capabilities": model.capabilities.model_dump(),
        "pricing": model.pricing.model_dump(),
        "extra_body": model.extra_body,
    }


def run_models_command(argv: list[str]) -> int:
    args = _build_models_parser().parse_args(argv)
    config = load_config_or_exit()

    match args.models_command:
        case "list":
            if args.json:
                print(
                    json.dumps({
                        "current_model": config.active_model,
                        "models": [
                            _model_catalog_entry(model) for model in config.models
                        ],
                    })
                )
                return 0

            table = Table(title="Model catalog")
            table.add_column("alias")
            table.add_column("provider")
            table.add_column("context")
            table.add_column("pricing (in/out)")
            for model in config.models:
                table.add_row(
                    model.alias,
                    model.provider,
                    f"{model.context_window:,}",
                    f"{model.pricing.input_per_million}/"
                    f"{model.pricing.output_per_million}",
                )
            rprint(table)
            return 0
        case "validate":
            errors = validate_models(config.models, config.providers)
            snapshot_errors, snapshot_warnings = snapshot_problems(
                load_model_snapshot(), now=datetime.now(UTC)
            )
            if args.strict:
                snapshot_errors = [*snapshot_errors, *snapshot_warnings]
                snapshot_warnings = []
            for warning in snapshot_warnings:
                print(f"warning: {warning}", file=sys.stderr)
            for error in [*errors, *snapshot_errors]:
                print(error, file=sys.stderr)
            if errors or snapshot_errors:
                return 1
            print(f"Validated {len(config.models)} models: OK")
            return 0

    return 1


def _build_packages_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqcode",
        description="Manage resource packages (tools, skills, prompts, agents, hooks)",
    )
    subparsers = parser.add_subparsers(dest="packages_command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="Install a package from a git URL or a local directory"
    )
    install_parser.add_argument("source", metavar="SOURCE")
    install_parser.add_argument(
        "--name", metavar="NAME", help="Override the installed package name"
    )

    subparsers.add_parser("list", help="List installed packages")

    remove_parser = subparsers.add_parser("remove", help="Remove an installed package")
    remove_parser.add_argument("name", metavar="NAME")

    update_parser = subparsers.add_parser(
        "update", help="Update a git-installed package (git pull)"
    )
    update_parser.add_argument("name", metavar="NAME")
    return parser


def _package_entry(package: InstalledPackage) -> dict[str, Any]:
    return {
        "name": package.name,
        "version": package.version,
        "path": str(package.path),
        "source": package.source,
        "git": package.git,
    }


def run_packages_command(argv: list[str]) -> int:
    args = _build_packages_parser().parse_args(argv)
    config = load_config_or_exit()

    try:
        match args.packages_command:
            case "install":
                info = install(args.source, args.name)
                names = list(dict.fromkeys([*config.packages, info.name]))
                VibeConfig.set_packages(names)
                print(json.dumps(_package_entry(info)))
                return 0
            case "list":
                installed = list_packages(config.packages)
                print(json.dumps([_package_entry(pkg) for pkg in installed]))
                return 0
            case "remove":
                remove(args.name)
                names = [name for name in config.packages if name != args.name]
                VibeConfig.set_packages(names)
                print(json.dumps({"removed": args.name}))
                return 0
            case "update":
                output = update(args.name)
                print(json.dumps({"updated": args.name, "output": output}))
                return 0
            case _:
                return 1
    except PackageError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


def _handle_subcommands() -> None:
    """Dispatch package/session/model/MCP subcommands before parsing flags."""
    match sys.argv[1:2]:
        case ["sessions"]:
            sys.exit(run_sessions_command(sys.argv[2:]))
        case ["models"]:
            sys.exit(run_models_command(sys.argv[2:]))
        case ["packages"]:
            sys.exit(run_packages_command(sys.argv[2:]))
        case ["mcp"]:
            sys.exit(run_mcp_command(sys.argv[2:]))
        case ["login"]:
            sys.exit(run_login_command(sys.argv[2:]))
        case ["logout"]:
            sys.exit(run_logout_command(sys.argv[2:]))
        case ["install"] | ["list"] | ["remove"] | ["update"]:
            sys.exit(run_packages_command(sys.argv[1:]))
        case _:
            return


def _handle_setup_and_login(args: argparse.Namespace) -> None:
    if args.setup:
        run_onboarding()
        sys.exit(0)

    if not args.login:
        return

    match args.login:
        case "anthropic":
            from vibe.cli.login import login_anthropic

            login_anthropic()
        case "qwen":
            from vibe.cli.login import login_qwen

            login_qwen()
    sys.exit(0)


def _ensure_default_files() -> None:
    """Create the config, instructions and history files on first run."""
    if not CONFIG_FILE.exists():
        try:
            VibeConfig.save_updates(VibeConfig.create_default())
        except Exception as e:
            rprint(f"[yellow]Could not create default config file: {e}[/]")

    if not INSTRUCTIONS_FILE.exists():
        try:
            INSTRUCTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            INSTRUCTIONS_FILE.touch()
        except Exception as e:
            rprint(f"[yellow]Could not create instructions file: {e}[/]")

    if not HISTORY_FILE.exists():
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text("Hello Vibe!\n", "utf-8")
        except Exception as e:
            rprint(f"[yellow]Could not create history file: {e}[/]")


def _resolve_pending_trust(args: argparse.Namespace) -> ProjectResources | None:
    """Ask for project trust in the interactive UI; never prompt headless."""
    if args.prompt is not None or args.trust is not None:
        return None
    return pending_trust(
        Path.cwd(),
        store=TrustStore.load(),
        default_project_trust=read_default_project_trust(),
    )


def _model_exists(config: VibeConfig, alias: str) -> bool:
    return any(model.alias == alias for model in config.models)


def _apply_model_override(config: VibeConfig, args: argparse.Namespace) -> None:
    """Apply ``--model`` and ``QQCODE_MODEL_OVERRIDE`` to the active model."""
    if args.model:
        if not _model_exists(config, args.model):
            error_msg = {"error": f"Model '{args.model}' not found"}
            print(json.dumps(error_msg), file=sys.stderr)
            sys.exit(1)
        os.environ["QQCODE_MODEL_OVERRIDE"] = args.model

    override_model = os.environ.get("QQCODE_MODEL_OVERRIDE")
    if not override_model:
        return
    if not _model_exists(config, override_model):
        rprint(f"[red]Model '{override_model}' not found in configuration[/]")
        sys.exit(1)
    config.active_model = override_model


def _print_session_list(config: VibeConfig) -> None:
    if not config.session_logging.enabled:
        print(json.dumps([]))
        sys.exit(0)

    sessions = InteractionLogger.list_sessions(
        config.session_logging, limit=20, workdir=config.effective_workdir
    )
    result = [
        {
            "session_id": summary.get("session_id", ""),
            "end_time": summary.get("end_time", ""),
            "last_user_message": summary.get("last_user_message", ""),
        }
        for _, summary in sessions
    ]
    print(json.dumps(result))
    sys.exit(0)


def _print_session(config: VibeConfig, session_id: str) -> None:
    if not config.session_logging.enabled:
        error_msg = {"error": "Session logging is disabled"}
        print(json.dumps(error_msg), file=sys.stderr)
        sys.exit(1)

    session_path = InteractionLogger.find_session_by_id(
        session_id, config.session_logging
    )
    if not session_path:
        error_msg = {"error": f"Session '{session_id}' not found"}
        print(json.dumps(error_msg), file=sys.stderr)
        sys.exit(1)

    try:
        messages, metadata = InteractionLogger.load_session(session_path)
    except Exception as e:
        error_msg = {"error": f"Failed to load session: {e!s}"}
        print(json.dumps(error_msg), file=sys.stderr)
        sys.exit(1)

    result = {
        "metadata": metadata,
        "messages": [msg.model_dump(mode="json") for msg in messages],
    }
    print(json.dumps(result))
    sys.exit(0)


def _handle_inspection_flags(config: VibeConfig, args: argparse.Namespace) -> None:
    """Handle one-shot session/model inspection flags and exit."""
    if args.list_sessions:
        _print_session_list(config)
    if args.get_session:
        _print_session(config, args.get_session)
    if args.list_models:
        result = {
            "current_model": config.active_model,
            "models": [_model_catalog_entry(model) for model in config.models],
        }
        print(json.dumps(result))
        sys.exit(0)
    if args.get_model:
        result = {"current_model": config.active_model}
        print(json.dumps(result))
        sys.exit(0)


@dataclass
class _ResumeState:
    """Conversation restored from a saved session, handed to the UI."""

    messages: list[LLMMessage] | None = None
    session_info: ResumeSessionInfo | None = None
    session_id: str | None = None
    additional_directory_contexts: list[Path] | None = None
    thinking_level: ThinkingLevel | None = None
    resumed_name: str | None = None


def _restore_directory_contexts(metadata: dict[str, Any]) -> list[Path]:
    contexts: list[Path] = []
    for raw in metadata.get("additional_directory_contexts", []):
        try:
            context_path = Path(raw)
            if context_path.exists() and context_path.is_dir():
                contexts.append(context_path)
        except Exception:
            # Skip invalid paths
            continue
    return contexts


def _load_saved_session(config: VibeConfig, args: argparse.Namespace) -> _ResumeState:
    if not config.session_logging.enabled:
        rprint(
            "[red]Session logging is disabled. "
            "Enable it in config to use --continue or --resume[/]"
        )
        sys.exit(1)

    if args.continue_session:
        session_to_load = InteractionLogger.find_latest_session(config.session_logging)
        if not session_to_load:
            rprint(
                f"[red]No previous sessions found in "
                f"{config.session_logging.save_dir}[/]"
            )
            sys.exit(1)
    else:
        session_to_load = InteractionLogger.find_session_by_id(
            args.resume, config.session_logging
        )
        if not session_to_load:
            rprint(
                f"[red]Session '{args.resume}' not found in "
                f"{config.session_logging.save_dir}[/]"
            )
            sys.exit(1)

    try:
        messages, metadata = InteractionLogger.load_session(session_to_load)
    except Exception as e:
        rprint(f"[red]Failed to load session: {e}[/]")
        sys.exit(1)

    session_id = metadata.get("session_id")
    thinking_level, resumed_name = _restore_session_preferences(config, metadata)
    return _ResumeState(
        messages=messages,
        session_info=ResumeSessionInfo(
            type="continue" if args.continue_session else "resume",
            session_id=(session_id or "unknown")[:8],
            session_time=metadata.get("start_time", "unknown time"),
        ),
        session_id=session_id,
        additional_directory_contexts=_restore_directory_contexts(metadata),
        thinking_level=thinking_level,
        resumed_name=resumed_name,
    )


def _fork_saved_session(config: VibeConfig, args: argparse.Namespace) -> _ResumeState:
    if not config.session_logging.enabled:
        rprint("[red]Session logging is disabled. Enable it in config to use --fork[/]")
        sys.exit(1)

    store = JsonlSessionStore(config.session_logging)
    fork_source = (
        InteractionLogger.find_session_by_id(args.fork, config.session_logging)
        if args.fork
        else InteractionLogger.find_latest_session(config.session_logging)
    )
    if fork_source is None:
        rprint("[red]No session found to fork[/]")
        sys.exit(1)
    if fork_source.suffix != ".jsonl":
        rprint("[red]Only .jsonl sessions can be forked[/]")
        sys.exit(1)

    try:
        view = store.read(fork_source)
        if not view.entries:
            rprint("[red]Cannot fork an empty session[/]")
            sys.exit(1)
        forked_path = store.fork(
            fork_source, view.entries[-1].id, workdir=config.effective_workdir
        )
        messages, metadata = InteractionLogger.load_session(forked_path)
    except Exception as e:
        rprint(f"[red]Failed to fork session: {e}[/]")
        sys.exit(1)

    thinking_level, resumed_name = _restore_session_preferences(config, metadata)
    return _ResumeState(
        messages=messages,
        session_info=ResumeSessionInfo(
            type="resume",
            session_id=str(metadata.get("session_id", "unknown"))[:8],
            session_time=str(metadata.get("start_time", "unknown time")),
        ),
        session_id=metadata.get("session_id"),
        thinking_level=thinking_level,
        resumed_name=resumed_name,
    )


def _resolve_session_state(
    config: VibeConfig, args: argparse.Namespace
) -> _ResumeState:
    state = _ResumeState()
    if args.continue_session or args.resume:
        state = _load_saved_session(config, args)
    if args.fork is not None:
        forked = _fork_saved_session(config, args)
        forked.additional_directory_contexts = state.additional_directory_contexts
        state = forked
    return state


def _run_programmatic_mode(
    config: VibeConfig, args: argparse.Namespace, state: _ResumeState
) -> None:
    stdin_prompt = get_prompt_from_stdin() if not args.prompt else None
    programmatic_prompt = args.prompt or stdin_prompt
    if not programmatic_prompt:
        print("Error: No prompt provided for programmatic mode", file=sys.stderr)
        sys.exit(1)
    output_format = OutputFormat(args.output if hasattr(args, "output") else "text")

    # --auto-approve takes precedence for backward compatibility;
    # --mode allows explicit control; plan mode is the default.
    execution_mode = "plan"
    if args.auto_approve:
        execution_mode = "auto-approve"
    elif args.mode:
        execution_mode = args.mode

    try:
        final_response = run_programmatic(
            config=config,
            prompt=programmatic_prompt,
            max_turns=args.max_turns,
            max_price=args.max_price,
            output_format=output_format,
            previous_messages=state.messages,
            mode=execution_mode,
            session_id=state.session_id,
            session_name=args.name,
        )
        if final_response:
            print(final_response)
        sys.exit(0)
    except ConversationLimitException as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _run_agent(
    *,
    config: VibeConfig,
    args: argparse.Namespace,
    state: _ResumeState,
    pending: ProjectResources | None,
    prompt_template_dirs: list[Path],
) -> None:
    if args.prompt is not None:
        _run_programmatic_mode(config, args, state)
        return

    # For interactive mode, try reading from stdin
    stdin_prompt = get_prompt_from_stdin()
    run_textual_ui(
        config,
        auto_approve=args.auto_approve,
        enable_streaming=True,
        initial_prompt=args.initial_prompt or stdin_prompt,
        loaded_messages=state.messages,
        session_info=state.session_info,
        additional_directory_contexts=state.additional_directory_contexts,
        prompt_template_dirs=prompt_template_dirs,
        resume=UISessionOptions(
            name=args.name or state.resumed_name,
            thinking_level=state.thinking_level,
            trust_request=pending,
        ),
    )


def main() -> None:
    load_api_keys_from_env()
    _handle_subcommands()
    args = parse_arguments()
    _handle_setup_and_login(args)

    try:
        _ensure_default_files()
        pending = _resolve_pending_trust(args)
        config = load_config_or_exit(
            args.agent, trust=False if pending is not None else args.trust
        )
        prompt_template_dirs = apply_runtime_flags(config, args)

        _apply_model_override(config, args)
        _handle_inspection_flags(config, args)

        state = _resolve_session_state(config, args)
        _run_agent(
            config=config,
            args=args,
            state=state,
            pending=pending,
            prompt_template_dirs=prompt_template_dirs,
        )

    except (KeyboardInterrupt, EOFError):
        rprint("\n[dim]Bye![/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
