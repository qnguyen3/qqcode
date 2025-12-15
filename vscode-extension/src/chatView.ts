import * as vscode from 'vscode';
import { QQCodeBackend } from './qqcodeBackend';
import { StreamChunk } from './types/events';

export class ChatViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'qqcodeChatView';

    private view?: vscode.WebviewView;
    private backend: QQCodeBackend;
    private messageHistory: Array<{role: string; content: string}> = [];

    constructor(
        private readonly extensionUri: vscode.Uri,
        backend: QQCodeBackend
    ) {
        this.backend = backend;
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        token: vscode.CancellationToken
    ) {
        this.view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.extensionUri]
        };

        webviewView.webview.html = this.getHtmlContent(webviewView.webview);

        // Handle messages from webview
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'userMessage':
                    await this.handleUserMessage(data.text);
                    break;
                case 'cancelRequest':
                    this.backend.cancel();
                    break;
            }
        });
    }

    private async handleUserMessage(text: string) {
        // Add user message to UI
        this.addMessageToUI('user', text);
        this.messageHistory.push({ role: 'user', content: text });

        // Start streaming response
        let assistantMessage = '';

        try {
            const config = vscode.workspace.getConfiguration('qqcode');
            const autoApprove = config.get<boolean>('autoApprove', false);

            for await (const chunk of this.backend.streamPrompt(text, autoApprove)) {
                if (chunk.kind === 'text') {
                    assistantMessage += chunk.text;
                    this.updateAssistantMessage(chunk.accumulated);
                } else if (chunk.kind === 'tool_call') {
                    this.showToolCall(chunk.toolName, chunk.args);
                } else if (chunk.kind === 'tool_result') {
                    this.showToolResult(chunk.toolCallId, chunk.result, chunk.isError);
                } else if (chunk.kind === 'error') {
                    this.showError(chunk.message);
                }
            }

            this.messageHistory.push({ role: 'assistant', content: assistantMessage });
            this.finalizeAssistantMessage();
        } catch (error) {
            this.showError(`Error: ${error}`);
        }
    }

    private addMessageToUI(role: string, content: string) {
        this.view?.webview.postMessage({
            type: 'addMessage',
            role,
            content
        });
    }

    private updateAssistantMessage(content: string) {
        this.view?.webview.postMessage({
            type: 'updateAssistantMessage',
            content
        });
    }

    private finalizeAssistantMessage() {
        this.view?.webview.postMessage({
            type: 'finalizeAssistantMessage'
        });
    }

    private showToolCall(toolName: string, args: any) {
        this.view?.webview.postMessage({
            type: 'toolCall',
            toolName,
            args
        });
    }

    private showToolResult(toolCallId: string, result: string, isError: boolean) {
        this.view?.webview.postMessage({
            type: 'toolResult',
            toolCallId,
            result,
            isError
        });
    }

    private showError(message: string) {
        this.view?.webview.postMessage({
            type: 'error',
            message
        });
    }

    private getHtmlContent(webview: vscode.Webview): string {
        return `<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>QQCode Chat</title>
            <style>
                ${this.getChatStyles()}
            </style>
        </head>
        <body>
            <div id="chat-container">
                <div id="messages"></div>
            </div>
            <div id="input-container">
                <textarea id="user-input" placeholder="Ask QQCode..." rows="3"></textarea>
                <button id="send-btn">Send</button>
            </div>
            <script>
                ${this.getChatScript()}
            </script>
        </body>
        </html>`;
    }

    private getChatStyles(): string {
        return `
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

            body {
                display: flex;
                flex-direction: column;
                height: 100vh;
                font-family: var(--vscode-font-family);
                font-size: var(--vscode-font-size);
                background-color: var(--vscode-editor-background);
                color: var(--vscode-editor-foreground);
            }

            #chat-container {
                flex: 1;
                overflow-y: auto;
                padding: 12px;
                display: flex;
                flex-direction: column;
            }

            #messages {
                display: flex;
                flex-direction: column;
                gap: 12px;
            }

            .message {
                padding: 10px 12px;
                border-radius: 6px;
                max-width: 85%;
                word-wrap: break-word;
                line-height: 1.5;
            }

            .message.user {
                background-color: var(--vscode-input-background);
                border: 1px solid var(--vscode-input-border);
                align-self: flex-end;
                margin-left: auto;
            }

            .message.assistant {
                background-color: var(--vscode-editor-inactiveSelectionBackground);
                align-self: flex-start;
            }

            .message.streaming {
                opacity: 0.8;
            }

            .message.streaming::after {
                content: '▊';
                animation: blink 1s infinite;
            }

            @keyframes blink {
                0%, 50% { opacity: 1; }
                51%, 100% { opacity: 0; }
            }

            .tool-call {
                font-size: 0.9em;
                color: var(--vscode-descriptionForeground);
                background-color: var(--vscode-editor-lineHighlightBackground);
                padding: 6px 10px;
                border-radius: 4px;
                margin: 6px 0;
                font-family: var(--vscode-editor-font-family);
            }

            .tool-call-header {
                font-weight: 600;
                margin-bottom: 4px;
            }

            .error {
                background-color: var(--vscode-inputValidation-errorBackground);
                border: 1px solid var(--vscode-inputValidation-errorBorder);
                color: var(--vscode-errorForeground);
                padding: 8px 12px;
                border-radius: 4px;
                margin: 6px 0;
            }

            #input-container {
                padding: 12px;
                border-top: 1px solid var(--vscode-panel-border);
                display: flex;
                gap: 8px;
                background-color: var(--vscode-editor-background);
            }

            #user-input {
                flex: 1;
                padding: 8px 12px;
                background-color: var(--vscode-input-background);
                color: var(--vscode-input-foreground);
                border: 1px solid var(--vscode-input-border);
                border-radius: 4px;
                resize: vertical;
                font-family: var(--vscode-font-family);
                font-size: var(--vscode-font-size);
            }

            #user-input:focus {
                outline: 1px solid var(--vscode-focusBorder);
                outline-offset: -1px;
            }

            #send-btn {
                padding: 8px 20px;
                background-color: var(--vscode-button-background);
                color: var(--vscode-button-foreground);
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-weight: 600;
                transition: background-color 0.2s;
            }

            #send-btn:hover {
                background-color: var(--vscode-button-hoverBackground);
            }

            #send-btn:active {
                opacity: 0.8;
            }

            /* Code blocks in messages */
            .message pre {
                background-color: var(--vscode-textCodeBlock-background);
                padding: 8px;
                border-radius: 4px;
                overflow-x: auto;
                margin: 6px 0;
            }

            .message code {
                font-family: var(--vscode-editor-font-family);
                font-size: 0.9em;
            }
        `;
    }

    private getChatScript(): string {
        return `
            const vscode = acquireVsCodeApi();
            const messagesDiv = document.getElementById('messages');
            const userInput = document.getElementById('user-input');
            const sendBtn = document.getElementById('send-btn');

            let currentAssistantMessage = null;

            sendBtn.addEventListener('click', sendMessage);
            userInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    sendMessage();
                }
            });

            function sendMessage() {
                const text = userInput.value.trim();
                if (!text) return;

                vscode.postMessage({
                    type: 'userMessage',
                    text: text
                });

                userInput.value = '';
                userInput.style.height = 'auto';
            }

            // Auto-resize textarea
            userInput.addEventListener('input', () => {
                userInput.style.height = 'auto';
                userInput.style.height = userInput.scrollHeight + 'px';
            });

            window.addEventListener('message', event => {
                const message = event.data;

                switch (message.type) {
                    case 'addMessage':
                        addMessage(message.role, message.content);
                        break;
                    case 'updateAssistantMessage':
                        updateCurrentAssistantMessage(message.content);
                        break;
                    case 'finalizeAssistantMessage':
                        finalizeCurrentAssistantMessage();
                        break;
                    case 'toolCall':
                        showToolCall(message.toolName, message.args);
                        break;
                    case 'toolResult':
                        showToolResult(message.toolCallId, message.result, message.isError);
                        break;
                    case 'error':
                        showError(message.message);
                        break;
                }
            });

            function addMessage(role, content) {
                const msgDiv = document.createElement('div');
                msgDiv.className = 'message ' + role;
                msgDiv.textContent = content;
                messagesDiv.appendChild(msgDiv);
                scrollToBottom();

                if (role === 'assistant') {
                    currentAssistantMessage = msgDiv;
                    msgDiv.classList.add('streaming');
                }
            }

            function updateCurrentAssistantMessage(content) {
                if (!currentAssistantMessage) {
                    addMessage('assistant', content);
                } else {
                    currentAssistantMessage.textContent = content;
                    scrollToBottom();
                }
            }

            function finalizeCurrentAssistantMessage() {
                if (currentAssistantMessage) {
                    currentAssistantMessage.classList.remove('streaming');
                    currentAssistantMessage = null;
                }
            }

            function showToolCall(toolName, args) {
                const toolDiv = document.createElement('div');
                toolDiv.className = 'tool-call';

                const header = document.createElement('div');
                header.className = 'tool-call-header';
                header.textContent = '🔧 ' + toolName;

                const argsDiv = document.createElement('div');
                argsDiv.textContent = JSON.stringify(args, null, 2);

                toolDiv.appendChild(header);
                toolDiv.appendChild(argsDiv);
                messagesDiv.appendChild(toolDiv);
                scrollToBottom();
            }

            function showToolResult(toolCallId, result, isError) {
                const resultDiv = document.createElement('div');
                resultDiv.className = 'tool-call';
                if (isError) {
                    resultDiv.style.borderLeft = '3px solid var(--vscode-errorForeground)';
                }
                resultDiv.textContent = '→ ' + (result.length > 200 ? result.substring(0, 200) + '...' : result);
                messagesDiv.appendChild(resultDiv);
                scrollToBottom();
            }

            function showError(message) {
                const errorDiv = document.createElement('div');
                errorDiv.className = 'error';
                errorDiv.textContent = '❌ ' + message;
                messagesDiv.appendChild(errorDiv);
                scrollToBottom();
            }

            function scrollToBottom() {
                const container = document.getElementById('chat-container');
                container.scrollTop = container.scrollHeight;
            }

            // Focus input on load
            userInput.focus();
        `;
    }
}
