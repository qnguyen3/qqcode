import * as vscode from 'vscode';
import { QQCodeBackend } from './qqcodeBackend';
import { ChatViewProvider } from './chatView';
import { QQCodeStatusBar } from './statusBar';

let backend: QQCodeBackend | null = null;
let outputChannel: vscode.OutputChannel;
let statusBar: QQCodeStatusBar | null = null;

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

    // Initialize status bar
    statusBar = new QQCodeStatusBar();
    context.subscriptions.push({ dispose: () => statusBar?.dispose() });

    // Register chat view provider
    const chatProvider = new ChatViewProvider(context.extensionUri, backend, context, statusBar);
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

    // Register command: Explain Code
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.explainCode', async () => {
            await askWithPrompt('Explain this code in detail');
        })
    );

    // Register command: Fix Code
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.fixCode', async () => {
            await askWithPrompt('Fix any bugs or issues in this code');
        })
    );

    // Register command: Improve Code
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.improveCode', async () => {
            await askWithPrompt('Suggest improvements for this code');
        })
    );

    // Register command: Add Documentation
    context.subscriptions.push(
        vscode.commands.registerCommand('qqcode.addDocs', async () => {
            await askWithPrompt('Add documentation and comments to this code');
        })
    );

    outputChannel.appendLine('QQCode commands registered');
    vscode.window.showInformationMessage('QQCode extension loaded! Click the sparkle icon in the status bar to start.');
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

    await runPrompt(question);
}

async function askWithPrompt(prompt: string) {
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

    await runPrompt(prompt);
}

async function runPrompt(question: string) {
    const config = vscode.workspace.getConfiguration('qqcode');
    const autoApprove = config.get<boolean>('autoApprove', false);
    const mode = autoApprove ? 'auto-approve' : 'plan';

    outputChannel.clear();
    outputChannel.show(true);
    outputChannel.appendLine(`[User] ${question}`);
    outputChannel.appendLine(`[QQCode] Processing...\n`);

    statusBar?.setThinking();
    let response = '';

    try {
        for await (const chunk of backend!.streamPrompt(question, mode)) {
            if (chunk.kind === 'text') {
                response += chunk.text;
                outputChannel.append(chunk.text);
                statusBar?.setStreaming();
            } else if (chunk.kind === 'tool_call') {
                outputChannel.appendLine(`\n[Tool] ${chunk.toolName}(${JSON.stringify(chunk.args)})`);
                statusBar?.setRunningTool(chunk.toolName);
            } else if (chunk.kind === 'tool_result') {
                const resultPreview = chunk.result.length > 100
                    ? chunk.result.substring(0, 100) + '...'
                    : chunk.result;
                outputChannel.appendLine(`[Result] ${resultPreview}`);
            } else if (chunk.kind === 'tool_approval_required') {
                statusBar?.setWaitingApproval(chunk.toolName);
            } else if (chunk.kind === 'plan_approval_required') {
                statusBar?.setPlanReview();
            } else if (chunk.kind === 'thinking') {
                statusBar?.setThinking();
            } else if (chunk.kind === 'error') {
                outputChannel.appendLine(`\n[Error] ${chunk.message}`);
                statusBar?.setError(chunk.message);
            }
        }

        outputChannel.appendLine('\n\n[QQCode] Done!');
        statusBar?.setReady();
        vscode.window.showInformationMessage('QQCode response complete! Check Output panel.');
    } catch (error) {
        outputChannel.appendLine(`\n[Error] ${error}`);
        statusBar?.setError(String(error));
        vscode.window.showErrorMessage(`QQCode error: ${error}`);
    }
}

export function deactivate() {
    if (backend) {
        backend.cancel();
    }
    if (statusBar) {
        statusBar.dispose();
    }
}
