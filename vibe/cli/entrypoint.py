from __future__ import annotations

import argparse
import json
import os
import sys

from rich import print as rprint

from vibe.cli.textual_ui.app import run_textual_ui
from vibe.core.config import (
    CONFIG_FILE,
    HISTORY_FILE,
    INSTRUCTIONS_FILE,
    MissingAPIKeyError,
    MissingPromptFileError,
    VibeConfig,
    load_api_keys_from_env,
)
from vibe.core.interaction_logger import InteractionLogger
from vibe.core.programmatic import run_programmatic
from vibe.core.types import OutputFormat, ResumeSessionInfo
from vibe.core.utils import ConversationLimitException
from vibe.setup.onboarding import run_onboarding


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the QQcode interactive CLI")
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
        "--enabled-tools",
        action="append",
        metavar="TOOL",
        help="Enable specific tools. In programmatic mode (-p), this disables "
        "all other tools. "
        "Can use exact names, glob patterns (e.g., 'bash*'), or "
        "regex with 're:' prefix. Can be specified multiple times.",
    )
    parser.add_argument(
        "--output",
        type=str,
        choices=["text", "json", "streaming", "vscode"],
        default="text",
        help="Output format for programmatic mode (-p): 'text' "
        "for human-readable (default), 'json' for all messages at end, "
        "'streaming' for newline-delimited JSON per message, "
        "'vscode' for structured streaming events (VSCode extension).",
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


def load_config_or_exit(agent: str | None = None) -> VibeConfig:
    try:
        return VibeConfig.load(agent)
    except MissingAPIKeyError:
        run_onboarding()
        return VibeConfig.load(agent)
    except MissingPromptFileError as e:
        rprint(f"[yellow]Invalid system prompt id: {e}[/]")
        sys.exit(1)
    except ValueError as e:
        rprint(f"[yellow]{e}[/]")
        sys.exit(1)


def main() -> None:  # noqa: PLR0912, PLR0915
    load_api_keys_from_env()
    args = parse_arguments()

    if args.setup:
        run_onboarding()
        sys.exit(0)

    if args.login:
        if args.login == "anthropic":
            from vibe.cli.login import login_anthropic

            login_anthropic()
        elif args.login == "qwen":
            from vibe.cli.login import login_qwen

            login_qwen()
        sys.exit(0)
    try:
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

        config = load_config_or_exit(args.agent)

        if args.enabled_tools:
            config.enabled_tools = args.enabled_tools

        # Handle model override from environment variable
        if 'QQCODE_MODEL_OVERRIDE' in os.environ:
            # Validate model exists
            override_model = os.environ['QQCODE_MODEL_OVERRIDE']
            model_exists = any(model.alias == override_model for model in config.models)
            if not model_exists:
                rprint(f"[red]Model '{override_model}' not found in configuration[/]")
                sys.exit(1)
            # Temporarily override the active model
            config.active_model = override_model

        # Handle --list-sessions flag
        if args.list_sessions:
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
                    "last_user_message": summary.get("last_user_message", "")
                }
                for _, summary in sessions
            ]
            print(json.dumps(result))
            sys.exit(0)

        # Handle --get-session flag
        if args.get_session:
            if not config.session_logging.enabled:
                error_msg = {"error": "Session logging is disabled"}
                print(json.dumps(error_msg), file=sys.stderr)
                sys.exit(1)

            session_path = InteractionLogger.find_session_by_id(args.get_session, config.session_logging)
            if not session_path:
                error_msg = {"error": f"Session '{args.get_session}' not found"}
                print(json.dumps(error_msg), file=sys.stderr)
                sys.exit(1)

            try:
                messages, metadata = InteractionLogger.load_session(session_path)
                result = {
                    "metadata": metadata,
                    "messages": [msg.model_dump(mode="json") for msg in messages]
                }
                print(json.dumps(result))
                sys.exit(0)
            except Exception as e:
                error_msg = {"error": f"Failed to load session: {str(e)}"}
                print(json.dumps(error_msg), file=sys.stderr)
                sys.exit(1)

        # Handle --list-models flag
        if args.list_models:
            result = {
                "current_model": config.active_model,
                "models": [
                    {
                        "alias": model.alias,
                        "name": model.name,
                        "provider": model.provider,
                        "context_limit": model.context_limit,
                        "input_price": model.input_price,
                        "output_price": model.output_price,
                        "extra_body": model.extra_body
                    }
                    for model in config.models
                ]
            }
            print(json.dumps(result))
            sys.exit(0)

        # Handle --get-model flag
        if args.get_model:
            result = {
                "current_model": config.active_model
            }
            print(json.dumps(result))
            sys.exit(0)

        # Handle model override for session
        if args.model:
            # Validate model exists
            model_exists = any(model.alias == args.model for model in config.models)
            if not model_exists:
                error_msg = {"error": f"Model '{args.model}' not found"}
                print(json.dumps(error_msg), file=sys.stderr)
                sys.exit(1)

            # Set environment variable for temporary override
            os.environ['QQCODE_MODEL_OVERRIDE'] = args.model

        # Handle model override from environment variable (set by --model flag or passed directly)
        if 'QQCODE_MODEL_OVERRIDE' in os.environ:
            # Validate model exists
            override_model = os.environ['QQCODE_MODEL_OVERRIDE']
            model_exists = any(model.alias == override_model for model in config.models)
            if not model_exists:
                rprint(f"[red]Model '{override_model}' not found in configuration[/]")
                sys.exit(1)
            # Temporarily override the active model
            config.active_model = override_model

        loaded_messages = None
        session_info = None
        full_session_id = None

        if args.continue_session or args.resume:
            if not config.session_logging.enabled:
                rprint(
                    "[red]Session logging is disabled. "
                    "Enable it in config to use --continue or --resume[/]"
                )
                sys.exit(1)

            session_to_load = None
            if args.continue_session:
                session_to_load = InteractionLogger.find_latest_session(
                    config.session_logging
                )
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
                loaded_messages, metadata = InteractionLogger.load_session(
                    session_to_load
                )
                full_session_id = metadata.get("session_id")
                session_id_display = (full_session_id or "unknown")[:8]
                session_time = metadata.get("start_time", "unknown time")

                session_info = ResumeSessionInfo(
                    type="continue" if args.continue_session else "resume",
                    session_id=session_id_display,
                    session_time=session_time,
                )
            except Exception as e:
                rprint(f"[red]Failed to load session: {e}[/]")
                sys.exit(1)

        # Only try to read from stdin if --prompt flag is used but empty,
        # or for interactive mode. Don't block on stdin.read() when prompt is provided.
        if args.prompt is not None:
            stdin_prompt = get_prompt_from_stdin() if not args.prompt else None
            programmatic_prompt = args.prompt or stdin_prompt
            if not programmatic_prompt:
                print(
                    "Error: No prompt provided for programmatic mode", file=sys.stderr
                )
                sys.exit(1)
            output_format = OutputFormat(
                args.output if hasattr(args, "output") else "text"
            )

            # Determine execution mode
            # --auto-approve flag takes precedence for backward compatibility
            # --mode flag allows explicit control
            execution_mode = "plan"  # default
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
                    previous_messages=loaded_messages,
                    mode=execution_mode,
                    session_id=full_session_id,
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
        else:
            # For interactive mode, try reading from stdin
            stdin_prompt = get_prompt_from_stdin()
            run_textual_ui(
                config,
                auto_approve=args.auto_approve,
                enable_streaming=True,
                initial_prompt=args.initial_prompt or stdin_prompt,
                loaded_messages=loaded_messages,
                session_info=session_info,
            )

    except (KeyboardInterrupt, EOFError):
        rprint("\n[dim]Bye![/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
