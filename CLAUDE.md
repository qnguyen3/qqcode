# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QQcode is an open-source CLI coding assistant. It provides a conversational interface to codebases with tools for file manipulation, code searching, version control, and command execution.

## Development Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the CLI locally
uv run qqcode

# Run all tests (parallel by default)
uv run pytest

# Run a specific test file
uv run pytest tests/test_agent_tool_call.py

# Run tests with verbose output
uv run pytest -v

# Linting (check only)
uv run ruff check .

# Linting (auto-fix)
uv run ruff check --fix .

# Format code
uv run ruff format .

# Type checking
uv run pyright

# Run all pre-commit hooks
uv run pre-commit run --all-files
```

## Architecture

### Package Structure

- **`vibe/cli/`** - CLI entry point and Textual-based TUI
  - `entrypoint.py` - Main entry point (`qqcode` command)
  - `textual_ui/` - Textual app, widgets, and event handlers
  - `autocompletion/` - Path and slash command autocompletion

- **`vibe/core/`** - Core agent logic
  - `agent.py` - Main `Agent` class orchestrating LLM interactions and tool execution
  - `config.py` - `VibeConfig` settings loaded from `config.toml` with Pydantic
  - `tools/` - Tool system (base classes, manager, built-in tools, MCP integration)
  - `llm/` - LLM backend abstraction (supports multiple providers)
  - `middleware.py` - Request/response middleware (auto-compact, context warnings, limits)
  - `prompts/` - System prompt templates (markdown files)

- **`vibe/acp/`** - Agent Client Protocol server implementation
  - Exposes QQcode as an ACP-compatible agent

- **`vibe/setup/`** - First-run onboarding flow

### Key Components

**Agent (`vibe/core/agent.py`)**: Central orchestrator that manages conversation flow, streams LLM responses, executes tools, and handles middleware pipeline.

**Tool System (`vibe/core/tools/`)**:
- `BaseTool` - Generic base class using Pydantic for args/result validation
- `ToolManager` - Discovers and instantiates tools from built-in and custom directories
- Built-in tools: `bash`, `grep`, `read_file`, `write_file`, `search_replace`, `todo`
- MCP tools are dynamically created via proxy classes

**Configuration (`vibe/core/config.py`)**: Pydantic-based settings loaded from `~/.qqcode/config.toml` or `./.qqcode/config.toml`. Supports custom system prompts, agent configurations, MCP servers, and tool permissions.

**LLM Backends (`vibe/core/llm/backend/`)**: Factory pattern supporting multiple API providers.

## Code Style

- Python 3.12+ with modern type hints (`list`, `dict`, `|` for unions)
- Pydantic v2 for data validation - prefer `model_validate` over manual parsing
- Use `pathlib.Path` for file operations
- Use `match-case` for pattern matching
- Avoid deep nesting - use early returns and guard clauses
- All imports require `from __future__ import annotations`
- Line length: 88 characters
- Always use `uv run` to execute Python commands
