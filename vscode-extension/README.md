# QQCode VSCode Extension

AI coding assistant powered by QQCode.

## Features

- **Ask About Selection**: Select code and ask QQCode to explain it, find bugs, suggest improvements, etc.
- **Streaming Responses**: See QQCode's response stream in real-time
- **Tool Execution**: Watch QQCode use tools (bash, read_file, grep, etc.) to answer your questions
- **Configurable**: Set custom CLI path and auto-approve settings

## Requirements

- QQCode CLI installed and available in PATH
  - Install: `pip install qqcode` or `uv tool install qqcode`
  - Test: `qqcode --version`

## Installation

### For Development/Testing

1. Open this directory in VSCode
2. Press `F5` to launch Extension Development Host
3. In the new window, open a project and test the extension

### From .vsix Package

1. Build package: `npm run package`
2. Install: `code --install-extension qqcode-companion-0.1.0.vsix`

## Usage

### Ask About Selection

1. Open a file and select some code
2. Open Command Palette (`Cmd+Shift+P` or `Ctrl+Shift+P`)
3. Run: **QQCode: Ask About Selection**
4. Enter your question (e.g., "Explain this code")
5. View the response in the Output panel (View → Output → QQCode)

## Configuration

Settings available in VSCode preferences:

- `qqcode.commandPath`: Path to QQCode CLI executable
  - Default: `"qqcode"`
  - Example: `"/usr/local/bin/qqcode"` or `"uv run qqcode"`

- `qqcode.autoApprove`: Automatically approve all tool executions
  - Default: `false`
  - **Warning**: Set to `true` only if you trust the code you're working with

## How It Works

The extension spawns the QQCode CLI as a subprocess:

```bash
qqcode --prompt "Your question" --output vscode
```

It then parses the JSON streaming events and displays them in the Output panel.

## Development

### Project Structure

```
vscode-extension/
├── src/
│   ├── extension.ts          # Entry point
│   ├── qqcodeBackend.ts      # CLI communication
│   └── types/events.ts       # Event type definitions
├── dist/                     # Build output
├── package.json              # Extension manifest
├── tsconfig.json             # TypeScript config
└── esbuild.mjs              # Build script
```

### Build Commands

```bash
# Install dependencies
npm install

# Build extension
npm run bundle

# Watch mode (rebuild on changes)
npm run watch

# Package as .vsix
npm run package
```

### Testing

1. Make changes to `src/` files
2. Run `npm run bundle` to rebuild
3. Press `F5` to launch Extension Development Host
4. Test in the new window
5. Check Output panel → QQCode for logs

## Troubleshooting

### "Command 'qqcode' not found"

**Solution:** Set the full path in settings:
```json
{
  "qqcode.commandPath": "/absolute/path/to/qqcode"
}
```

Or ensure `qqcode` is in your PATH.

### No response appears

1. Check Output panel: View → Output → QQCode
2. Look for error messages
3. Test CLI directly: `qqcode --prompt "hello" --output vscode`

### Extension doesn't activate

1. Open Developer Tools: Help → Toggle Developer Tools
2. Check Console for errors
3. Verify `dist/extension.js` exists
4. Try rebuilding: `npm run bundle`

## Roadmap

### Phase 3: Chat UI (Planned)
- Webview-based chat interface
- Message history
- Better UI than Output panel

### Phase 4: Advanced Features (Planned)
- Tool approval modal
- Status bar integration
- Context menu items ("Ask QQCode about this file")
- Inline chat widget

### Phase 5: Publishing (Planned)
- VSCode Marketplace
- Open VSX Registry

## License

Same as QQCode main project

## Links

- [QQCode Repository](https://github.com/qnguyen3/qqcode)
- [VSCode Extension API](https://code.visualstudio.com/api)
