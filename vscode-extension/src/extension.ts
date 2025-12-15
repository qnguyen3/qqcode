import * as vscode from 'vscode';
import { QQCodeBackend } from './qqcodeBackend';
import { ChatViewProvider } from './chatView';

let backend: QQCodeBackend | null = null;
let outputChannel: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext) {
    console.log('QQCode extension activated');

    outputChannel = vscode.window.createOutputChannel('QQCode');
    outputChannel.appendLine('QQCode extension activated');

    // Get configuration
    const config = vscode.workspace.getConfiguration('qqcode');
    const commandPath = config.get<string>('commandPath', 'qqcode');
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

    // Initialize backend
    backend = new QQCodeBackend(commandPath, workspaceRoot, outputChannel);
    outputChannel.appendLine(`QQCode CLI path: ${commandPath}`);
    outputChannel.appendLine(`Workspace root: ${workspaceRoot}`);

    // Register chat view provider
    const chatProvider = new ChatViewProvider(context.extensionUri, backend, context);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            ChatViewProvider.viewType,
            chatProvider
        )
    );

    // Register command: Open Chat
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.openChat', () => {
            vscode.commands.executeCommand('qqcodeChatView.focus');
        })
    );

    // Register command: Ask About Selection
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.askSelection', async () => {
            await askAboutSelection();
        })
    );

    outputChannel.appendLine('QQCode commands registered');
    vscode.window.showInformationMessage('QQCode extension loaded! Check the QQCode sidebar for chat.');
}

async function askAboutSelection() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const selection = editor.document.getText(editor.selection);
    if (!selection) {
        vscode.window.showErrorMessage('No text selected');
        return;
    }

    const question = await vscode.window.showInputBox({
        prompt: 'What do you want to ask about the selection?',
        placeHolder: 'e.g., Explain this code'
    });

    if (!question) {
        return;
    }

    const config = vscode.workspace.getConfiguration('qqcode');
    const autoApprove = config.get<boolean>('autoApprove', false);

    outputChannel.clear();
    outputChannel.show(true);
    outputChannel.appendLine(`[User] ${question}`);
    outputChannel.appendLine(`[QQCode] Processing...\n`);

    let response = '';

    try {
        for await (const chunk of backend!.streamPrompt(question, autoApprove)) {
            if (chunk.kind === 'text') {
                response += chunk.text;
                outputChannel.append(chunk.text);
            } else if (chunk.kind === 'tool_call') {
                outputChannel.appendLine(`\n[Tool] ${chunk.toolName}(${JSON.stringify(chunk.args)})`);
            } else if (chunk.kind === 'tool_result') {
                const resultPreview = chunk.result.length > 100
                    ? chunk.result.substring(0, 100) + '...'
                    : chunk.result;
                outputChannel.appendLine(`[Result] ${resultPreview}`);
            } else if (chunk.kind === 'thinking') {
                // Optionally show thinking content
                // outputChannel.append(chunk.text);
            } else if (chunk.kind === 'error') {
                outputChannel.appendLine(`\n[Error] ${chunk.message}`);
            }
        }

        outputChannel.appendLine('\n\n[QQCode] Done!');
        vscode.window.showInformationMessage('QQCode response complete! Check Output panel.');
    } catch (error) {
        outputChannel.appendLine(`\n[Error] ${error}`);
        vscode.window.showErrorMessage(`QQCode error: ${error}`);
    }
}

export function deactivate() {
    if (backend) {
        backend.cancel();
    }
}
