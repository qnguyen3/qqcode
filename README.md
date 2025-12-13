# QQcode

[![PyPI Version](https://img.shields.io/pypi/v/qqcode)](https://pypi.org/project/qqcode)
[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/release/python-3120/)
[![CI Status](https://github.com/qnguyen3/qqcode/actions/workflows/ci.yml/badge.svg)](https://github.com/qnguyen3/qqcode/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/qnguyen3/qqcode)](https://github.com/qnguyen3/qqcode/blob/main/LICENSE)

```
  ▇▇▇▇      ▇▇▇▇
▇▇    ▇▇  ▇▇    ▇▇
▇▇    ▇▇  ▇▇    ▇▇
▇▇  ▇▇▇▇  ▇▇  ▇▇▇▇
  ▇▇▇▇▇▇    ▇▇▇▇▇▇
```

**Open-source CLI coding assistant.**

QQcode is a command-line coding assistant powered by AI models. It provides a conversational interface to your codebase, allowing you to use natural language to explore, modify, and interact with your projects through a powerful set of tools.

> [!WARNING]
> QQcode works on Windows, but we officially support and target UNIX environments.

### One-line install (recommended)

**Linux and macOS**

```bash
curl -LsSf https://raw.githubusercontent.com/qnguyen3/qqcode/main/scripts/install.sh | bash
```

**Windows**

First, install uv
```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then, use uv command below.

### Using uv

```bash
uv tool install qqcode
```

### Using pip

```bash
pip install qqcode
```

## Features

- **Interactive Chat**: A conversational AI agent that understands your requests and breaks down complex tasks.
- **Powerful Toolset**: A suite of tools for file manipulation, code searching, version control, and command execution, right from the chat prompt.
  - Read, write, and patch files (`read_file`, `write_file`, `search_replace`).
  - Execute shell commands in a stateful terminal (`bash`).
  - Recursively search code with `grep` (with `ripgrep` support).
  - Manage a `todo` list to track the agent's work.
- **Project-Aware Context**: QQcode automatically scans your project's file structure and Git status to provide relevant context to the agent, improving its understanding of your codebase.
- **Advanced CLI Experience**: Built with modern libraries for a smooth and efficient workflow.
  - Autocompletion for slash commands (`/`) and file paths (`@`).
  - Persistent command history.
  - Beautiful Themes.
- **Highly Configurable**: Customize models, providers, tool permissions, and UI preferences through a simple `config.toml` file.
- **Safety First**: Features tool execution approval.

## Quick Start

1. Navigate to your project's root directory:

   ```bash
   cd /path/to/your/project
   ```

2. Run QQcode:

   ```bash
   qqcode
   ```

3. If this is your first time running QQcode, it will:

   - Create a default configuration file at `~/.qqcode/config.toml`
   - Prompt you to enter your API key if it's not already configured
   - Save your API key to `~/.qqcode/.env` for future use

4. Start interacting with the agent!

   ```
   > Can you find all instances of the word "TODO" in the project?

   🤖 The user wants to find all instances of "TODO". The `grep` tool is perfect for this. I will use it to search the current directory.

   > grep(pattern="TODO", path=".")

   ... (grep tool output) ...

   🤖 I found the following "TODO" comments in your project.
   ```

## Usage

### Interactive Mode

Simply run `qqcode` to enter the interactive chat loop.

- **Multi-line Input**: Press `Ctrl+J` or `Shift+Enter` for select terminals to insert a newline.
- **File Paths**: Reference files in your prompt using the `@` symbol for smart autocompletion (e.g., `> Read the file @src/agent.py`).
- **Shell Commands**: Prefix any command with `!` to execute it directly in your shell, bypassing the agent (e.g., `> !ls -l`).

You can start QQcode with a prompt with the following command:

```bash
qqcode "Refactor the main function in cli/main.py to be more modular."
```

**Note**: The `--auto-approve` flag automatically approves all tool executions without prompting. In interactive mode, you can also toggle auto-approve on/off using `Shift+Tab`.

### Programmatic Mode

You can run QQcode non-interactively by piping input or using the `--prompt` flag. This is useful for scripting.

```bash
qqcode --prompt "Refactor the main function in cli/main.py to be more modular."
```

by default it will use `auto-approve` mode.

### Slash Commands

Use slash commands for meta-actions and configuration changes during a session.

## Configuration

QQcode is configured via a `config.toml` file. It looks for this file first in `./.qqcode/config.toml` and then falls back to `~/.qqcode/config.toml`.

### API Key Configuration

QQcode supports multiple ways to configure your API keys:

1. **Interactive Setup (Recommended for first-time users)**: When you run QQcode for the first time or if your API key is missing, QQcode will prompt you to enter it. The key will be securely saved to `~/.qqcode/.env` for future sessions.

2. **Environment Variables**: Set your API key as an environment variable:

   ```bash
   export MISTRAL_API_KEY="your_mistral_api_key"
   ```

3. **`.env` File**: Create a `.env` file in `~/.qqcode/` and add your API keys:

   ```bash
   MISTRAL_API_KEY=your_mistral_api_key
   ```

   QQcode automatically loads API keys from `~/.qqcode/.env` on startup. Environment variables take precedence over the `.env` file if both are set.

**Note**: The `.env` file is specifically for API keys and other provider credentials. General QQcode configuration should be done in `config.toml`.

### Custom System Prompts

You can create custom system prompts to replace the default one (`prompts/cli.md`). Create a markdown file in the `~/.qqcode/prompts/` directory with your custom prompt content.

To use a custom system prompt, set the `system_prompt_id` in your configuration to match the filename (without the `.md` extension):

```toml
# Use a custom system prompt
system_prompt_id = "my_custom_prompt"
```

This will load the prompt from `~/.qqcode/prompts/my_custom_prompt.md`.

### Custom Agent Configurations

You can create custom agent configurations for specific use cases (e.g., red-teaming, specialized tasks) by adding agent-specific TOML files in the `~/.qqcode/agents/` directory.

To use a custom agent, run QQcode with the `--agent` flag:

```bash
qqcode --agent my_custom_agent
```

QQcode will look for a file named `my_custom_agent.toml` in the agents directory and apply its configuration.

Example custom agent configuration (`~/.qqcode/agents/redteam.toml`):

```toml
# Custom agent configuration for red-teaming
active_model = "devstral-2"
system_prompt_id = "redteam"

# Disable some tools for this agent
disabled_tools = ["search_replace", "write_file"]

# Override tool permissions for this agent
[tools.bash]
permission = "always"

[tools.read_file]
permission = "always"
```

Note: this implies that you have setup a redteam prompt names `~/.qqcode/prompts/redteam.md`

### MCP Server Configuration

You can configure MCP (Model Context Protocol) servers to extend QQcode's capabilities. Add MCP server configurations under the `mcp_servers` section:

```toml
# Example MCP server configurations
[[mcp_servers]]
name = "my_http_server"
transport = "http"
url = "http://localhost:8000"
headers = { "Authorization" = "Bearer my_token" }
api_key_env = "MY_API_KEY_ENV_VAR"
api_key_header = "Authorization"
api_key_format = "Bearer {token}"

[[mcp_servers]]
name = "my_streamable_server"
transport = "streamable-http"
url = "http://localhost:8001"
headers = { "X-API-Key" = "my_api_key" }

[[mcp_servers]]
name = "fetch_server"
transport = "stdio"
command = "uvx"
args = ["mcp-server-fetch"]
```

Supported transports:

- `http`: Standard HTTP transport
- `streamable-http`: HTTP transport with streaming support
- `stdio`: Standard input/output transport (for local processes)

Key fields:

- `name`: A short alias for the server (used in tool names)
- `transport`: The transport type
- `url`: Base URL for HTTP transports
- `headers`: Additional HTTP headers
- `api_key_env`: Environment variable containing the API key
- `command`: Command to run for stdio transport
- `args`: Additional arguments for stdio transport

MCP tools are named using the pattern `{server_name}_{tool_name}` and can be configured with permissions like built-in tools:

```toml
# Configure permissions for specific MCP tools
[tools.fetch_server_get]
permission = "always"

[tools.my_http_server_query]
permission = "ask"
```

### Enable/disable tools with patterns

You can control which tools are active using `enabled_tools` and `disabled_tools`.
These fields support exact names, glob patterns, and regular expressions.

Examples:

```toml
# Only enable tools that start with "serena_" (glob)
enabled_tools = ["serena_*"]

# Regex (prefix with re:) — matches full tool name (case-insensitive)
enabled_tools = ["re:^serena_.*$"]

# Heuristic regex support (patterns like `serena.*` are treated as regex)
enabled_tools = ["serena.*"]

# Disable a group with glob; everything else stays enabled
disabled_tools = ["mcp_*", "grep"]
```

Notes:

- MCP tool names use underscores, e.g., `serena_list` not `serena.list`.
- Regex patterns are matched against the full tool name using fullmatch.

### Custom QQcode Home Directory

By default, QQcode stores its configuration in `~/.qqcode/`. You can override this by setting the `QQCODE_HOME` environment variable:

```bash
export QQCODE_HOME="/path/to/custom/qqcode/home"
```

This affects where QQcode looks for:

- `config.toml` - Main configuration
- `.env` - API keys
- `agents/` - Custom agent configurations
- `prompts/` - Custom system prompts
- `tools/` - Custom tools
- `logs/` - Session logs

## Resources

- [CHANGELOG](CHANGELOG.md) - See what's new in each version
- [CONTRIBUTING](CONTRIBUTING.md) - Guidelines for feedback and bug reports

## License

Copyright 2025 QQcode Team

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the [LICENSE](LICENSE) file for the full license text.
