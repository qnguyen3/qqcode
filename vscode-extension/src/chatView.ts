import * as vscode from 'vscode';
import { QQCodeBackend } from './qqcodeBackend';
import { StreamChunk, SessionSummary, SessionData, ModelInfo } from './types/events';

export class ChatViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'qqcodeChatView';

    private view?: vscode.WebviewView;
    private backend: QQCodeBackend;
    private messageHistory: Array<{role: string; content: string}> = [];
    private currentSessionId: string | null = null;
    private availableSessions: SessionSummary[] = [];
    private currentModel: string | null = null;
    private availableModels: ModelInfo[] = [];
    private extensionContext: vscode.ExtensionContext;

    // State management for webview communication
    private webviewReady: boolean = false;
    private pendingMessages: any[] = [];
    private initializationPromise: Promise<void> | null = null;

    constructor(
        private readonly extensionUri: vscode.Uri,
        backend: QQCodeBackend,
        extensionContext: vscode.ExtensionContext
    ) {
        this.backend = backend;
        this.extensionContext = extensionContext;

        // Restore persisted state
        this.restoreState();
    }

    /**
     * Save state to workspace storage for persistence across webview recreations
     */
    private saveState(): void {
        this.extensionContext.workspaceState.update('qqcode.currentSessionId', this.currentSessionId);
        this.extensionContext.workspaceState.update('qqcode.messageHistory', this.messageHistory);
    }

    /**
     * Restore state from workspace storage
     */
    private restoreState(): void {
        this.currentSessionId = this.extensionContext.workspaceState.get<string | null>('qqcode.currentSessionId', null);
        this.messageHistory = this.extensionContext.workspaceState.get<Array<{role: string; content: string}>>('qqcode.messageHistory', []);

        // Also restore model from global state (already exists)
        const savedModel = this.extensionContext.globalState.get<string>('qqcode.selectedModel');
        if (savedModel) {
            this.currentModel = savedModel;
        }
    }

    resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        token: vscode.CancellationToken
    ) {
        this.view = webviewView;
        this.webviewReady = false;
        this.pendingMessages = [];

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.extensionUri]
        };

        webviewView.webview.html = this.getHtmlContent(webviewView.webview);

        // Handle messages from webview
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'webviewReady':
                    // Webview is now ready to receive messages
                    this.webviewReady = true;
                    this.flushPendingMessages();
                    // Now initialize the webview with data
                    await this.initializeWebview();
                    break;
                case 'userMessage':
                    await this.handleUserMessage(data.text);
                    break;
                case 'cancelRequest':
                    this.backend.cancel();
                    break;
                case 'newConversation':
                    await this.switchToSession(null);
                    break;
                case 'selectSession':
                    await this.switchToSession(data.sessionId);
                    break;
                case 'refreshSessions':
                    await this.refreshSessionsList();
                    break;
                case 'selectModel':
                    await this.switchModel(data.modelAlias);
                    break;
            }
        });

        // Handle visibility changes - refresh data when view becomes visible again
        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible && this.webviewReady) {
                // Refresh data when view becomes visible
                this.refreshSessionsList();
                this.refreshModelsList();
            }
        });

        // Handle webview disposal
        webviewView.onDidDispose(() => {
            this.webviewReady = false;
            this.view = undefined;
        });
    }

    /**
     * Initialize the webview with all necessary data after it signals ready
     */
    private async initializeWebview(): Promise<void> {
        // Load sessions and models in parallel
        const sessionsPromise = this.refreshSessionsList();
        const modelsPromise = this.refreshModelsList();

        await Promise.all([sessionsPromise, modelsPromise]);

        // Restore conversation state if we have a current session
        if (this.currentSessionId && this.messageHistory.length > 0) {
            // Restore messages to UI
            for (const msg of this.messageHistory) {
                this.addMessageToUI(msg.role, msg.content);
            }

            // Update session badge
            this.sendToWebview({
                type: 'updateCurrentSession',
                sessionId: this.currentSessionId
            });
        }

        // Restore model selection
        if (this.currentModel) {
            this.sendToWebview({
                type: 'updateCurrentModel',
                modelAlias: this.currentModel
            });
        }
    }

    /**
     * Flush pending messages that were queued before webview was ready
     */
    private flushPendingMessages(): void {
        for (const msg of this.pendingMessages) {
            this.view?.webview.postMessage(msg);
        }
        this.pendingMessages = [];
    }

    private async handleUserMessage(text: string) {
        // Add user message to UI
        this.addMessageToUI('user', text);
        this.messageHistory.push({ role: 'user', content: text });

        // Track the session we're trying to continue (if any)
        const requestedSessionId = this.currentSessionId;

        // Start streaming response
        let assistantMessage = '';

        try {
            const config = vscode.workspace.getConfiguration('qqcode');
            const autoApprove = config.get<boolean>('autoApprove', false);

            for await (const chunk of this.backend.streamPrompt(text, autoApprove, this.currentSessionId || undefined, this.currentModel || undefined)) {
                if (chunk.kind === 'session_started') {
                    const returnedSessionId = chunk.sessionId;

                    if (!this.currentSessionId) {
                        // New conversation - capture the session ID
                        this.currentSessionId = returnedSessionId;
                        console.log(`[QQCode] New session started: ${this.currentSessionId}`);
                    } else if (requestedSessionId && returnedSessionId !== requestedSessionId) {
                        // Session ID mismatch - this shouldn't happen with proper --resume handling
                        // but if it does, update to the new session ID
                        console.warn(`[QQCode] Session ID mismatch! Requested: ${requestedSessionId}, Got: ${returnedSessionId}`);
                        this.currentSessionId = returnedSessionId;
                    } else {
                        // Continuing existing session - verify it matches
                        console.log(`[QQCode] Continuing session: ${this.currentSessionId}`);
                    }

                    // Always update UI to reflect the current session
                    this.sendToWebview({
                        type: 'updateCurrentSession',
                        sessionId: this.currentSessionId
                    });
                } else if (chunk.kind === 'text') {
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

            // Save state after successful message exchange
            this.saveState();

            // Refresh sessions list after completing response
            await this.refreshSessionsList();
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

    /**
     * Load and update conversation list
     */
    private async refreshSessionsList(): Promise<void> {
        try {
            this.availableSessions = await this.backend.listSessions();
            this.sendToWebview({
                type: 'updateSessionsList',
                sessions: this.availableSessions
            });
        } catch (error) {
            console.error('Failed to load sessions:', error);
            // Hide conversation controls if session management is disabled
            this.sendToWebview({
                type: 'hideConversationControls',
                reason: error instanceof Error ? error.message : 'Unknown error'
            });
        }
    }

    /**
     * Switch to a different conversation
     */
    private async switchToSession(sessionId: string | null): Promise<void> {
        // If switching to the same session, just return
        if (sessionId === this.currentSessionId) {
            return;
        }

        this.currentSessionId = sessionId;

        // Clear current UI
        this.sendToWebview({ type: 'clearMessages' });
        this.messageHistory = [];

        if (sessionId) {
            // Load session history
            try {
                const sessionData = await this.backend.getSession(sessionId);
                if (sessionData) {
                    // Populate UI with history
                    for (const msg of sessionData.messages) {
                        if (msg.role === 'user' || msg.role === 'assistant') {
                            if (msg.content) {
                                this.addMessageToUI(msg.role, msg.content);
                                this.messageHistory.push({ role: msg.role, content: msg.content });
                            }
                        }
                    }
                }
            } catch (error) {
                console.error(`[QQCode] Failed to load session ${sessionId}:`, error);
                this.showError(`Failed to load conversation: ${error}`);
            }
        }

        // Update UI to show current session
        this.sendToWebview({
            type: 'updateCurrentSession',
            sessionId: this.currentSessionId
        });

        // Save the current session selection
        this.saveState();
    }

    /**
     * Load and update model list
     */
    private async refreshModelsList(): Promise<void> {
        try {
            this.availableModels = await this.backend.listModels();
            this.sendToWebview({
                type: 'updateModelsList',
                models: this.availableModels
            });

            // Get current model
            this.currentModel = await this.backend.getCurrentModel();
            this.sendToWebview({
                type: 'updateCurrentModel',
                modelAlias: this.currentModel
            });
        } catch (error) {
            console.error('Failed to load models:', error);
            this.sendToWebview({
                type: 'hideModelControls',
                reason: error instanceof Error ? error.message : 'Unknown error'
            });
        }
    }

    /**
     * Switch to a different model
     */
    private async switchModel(modelAlias: string): Promise<void> {
        // Skip if already on this model
        if (modelAlias === this.currentModel) {
            return;
        }

        this.currentModel = modelAlias;

        // Persist in global VSCode state (not workspace-specific)
        this.extensionContext.globalState.update('qqcode.selectedModel', modelAlias);

        // Update UI
        this.sendToWebview({
            type: 'updateCurrentModel',
            modelAlias: modelAlias
        });

        console.log(`[QQCode] Switched to model: ${modelAlias}`);
    }

    /**
     * Send message to webview, queuing if not ready
     */
    private sendToWebview(message: any): void {
        if (this.webviewReady && this.view) {
            this.view.webview.postMessage(message);
        } else {
            // Queue message to be sent when webview is ready
            this.pendingMessages.push(message);
        }
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
            <div id="model-controls" style="display: none;">
                <select id="model-selector">
                    <option value="">Loading models...</option>
                </select>
            </div>
            <div id="conversation-controls" style="display: none;">
                <div class="conversation-header">
                    <select id="session-selector">
                        <option value="">New Conversation</option>
                    </select>
                    <button id="new-conversation-btn" title="Start New Conversation">+</button>
                    <button id="refresh-sessions-btn" title="Refresh">⟳</button>
                </div>
                <div id="current-session-badge" style="display: none;">
                    Session: <span id="session-id-display"></span>
                </div>
            </div>
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

            #model-controls {
                padding: 4px 0;
            }

            #model-selector {
                width: 100%;
                padding: 4px 8px;
                background-color: var(--vscode-dropdown-background);
                color: var(--vscode-dropdown-foreground);
                border: 1px solid var(--vscode-dropdown-border);
                border-radius: 4px;
                font-family: var(--vscode-font-family);
                font-size: var(--vscode-font-size);
            }

            #model-selector:focus {
                outline: 1px solid var(--vscode-focusBorder);
                outline-offset: -1px;
            }

            #conversation-controls {
                padding: 8px 12px;
                border-bottom: 1px solid var(--vscode-panel-border);
                background-color: var(--vscode-editor-background);
            }

            .conversation-header {
                display: flex;
                gap: 8px;
                align-items: center;
            }

            #session-selector {
                flex: 1;
                padding: 6px 10px;
                background-color: var(--vscode-input-background);
                color: var(--vscode-input-foreground);
                border: 1px solid var(--vscode-input-border);
                border-radius: 4px;
                font-family: var(--vscode-font-family);
                font-size: var(--vscode-font-size);
            }

            #session-selector:focus {
                outline: 1px solid var(--vscode-focusBorder);
                outline-offset: -1px;
            }

            #new-conversation-btn, #refresh-sessions-btn {
                padding: 6px 12px;
                background-color: var(--vscode-button-background);
                color: var(--vscode-button-foreground);
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-family: var(--vscode-font-family);
                font-size: var(--vscode-font-size);
                display: flex;
                align-items: center;
                justify-content: center;
                min-width: 36px;
            }

            #new-conversation-btn:hover, #refresh-sessions-btn:hover {
                background-color: var(--vscode-button-hoverBackground);
            }

            #current-session-badge {
                font-size: 0.85em;
                color: var(--vscode-descriptionForeground);
                margin-top: 4px;
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
            const modelSelector = document.getElementById('model-selector');
            const modelControls = document.getElementById('model-controls');
            const sessionSelector = document.getElementById('session-selector');
            const newConversationBtn = document.getElementById('new-conversation-btn');
            const refreshSessionsBtn = document.getElementById('refresh-sessions-btn');
            const conversationControls = document.getElementById('conversation-controls');
            const currentSessionBadge = document.getElementById('current-session-badge');
            const sessionIdDisplay = document.getElementById('session-id-display');

            let currentAssistantMessage = null;

            // Track UI state for preservation
            let uiState = {
                modelControlsVisible: false,
                conversationControlsVisible: false,
                currentSessionId: null,
                currentModel: null
            };

            // Restore state if available (for webview persistence)
            const previousState = vscode.getState();
            if (previousState) {
                uiState = { ...uiState, ...previousState };
                // Apply restored visibility state
                if (uiState.modelControlsVisible) {
                    modelControls.style.display = 'block';
                }
                if (uiState.conversationControlsVisible) {
                    conversationControls.style.display = 'block';
                }
                if (uiState.currentSessionId) {
                    sessionSelector.value = uiState.currentSessionId;
                    currentSessionBadge.style.display = 'block';
                    sessionIdDisplay.textContent = uiState.currentSessionId;
                }
                if (uiState.currentModel) {
                    modelSelector.value = uiState.currentModel;
                }
            }

            // Save state helper
            function saveUIState() {
                vscode.setState(uiState);
            }

            // Model selector event listener
            modelSelector.addEventListener('change', (e) => {
                const modelAlias = e.target.value;
                if (modelAlias) {
                    uiState.currentModel = modelAlias;
                    saveUIState();
                    vscode.postMessage({
                        type: 'selectModel',
                        modelAlias: modelAlias
                    });
                }
            });

            // Conversation controls event listeners
            sessionSelector.addEventListener('change', (e) => {
                const sessionId = e.target.value;
                vscode.postMessage({
                    type: 'selectSession',
                    sessionId: sessionId || null
                });
            });

            newConversationBtn.addEventListener('click', () => {
                vscode.postMessage({ type: 'newConversation' });
            });

            refreshSessionsBtn.addEventListener('click', () => {
                vscode.postMessage({ type: 'refreshSessions' });
            });

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
                    case 'updateSessionsList':
                        updateSessionsDropdown(message.sessions);
                        conversationControls.style.display = 'block';
                        uiState.conversationControlsVisible = true;
                        saveUIState();
                        break;
                    case 'updateCurrentSession':
                        updateCurrentSessionBadge(message.sessionId);
                        uiState.currentSessionId = message.sessionId;
                        saveUIState();
                        break;
                    case 'clearMessages':
                        messagesDiv.innerHTML = '';
                        currentAssistantMessage = null;
                        break;
                    case 'hideConversationControls':
                        conversationControls.style.display = 'none';
                        uiState.conversationControlsVisible = false;
                        saveUIState();
                        showError('Session management disabled: ' + message.reason);
                        break;
                    case 'updateModelsList':
                        updateModelsDropdown(message.models);
                        modelControls.style.display = 'block';
                        uiState.modelControlsVisible = true;
                        saveUIState();
                        break;
                    case 'updateCurrentModel':
                        modelSelector.value = message.modelAlias || '';
                        uiState.currentModel = message.modelAlias;
                        saveUIState();
                        break;
                    case 'hideModelControls':
                        modelControls.style.display = 'none';
                        uiState.modelControlsVisible = false;
                        saveUIState();
                        showError('Model loading failed: ' + message.reason);
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

            function updateSessionsDropdown(sessions) {
                // Preserve current selection before updating
                const currentValue = sessionSelector.value;

                // Keep "New Conversation" option
                sessionSelector.innerHTML = '<option value="">New Conversation</option>';

                sessions.forEach(session => {
                    const option = document.createElement('option');
                    option.value = session.session_id;

                    // Format: "Preview text... (Dec 15, 10:30)"
                    const preview = session.last_user_message.substring(0, 40);
                    const date = new Date(session.end_time);
                    const timeStr = date.toLocaleString('en-US', {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit'
                    });

                    option.textContent = preview + '... (' + timeStr + ')';
                    sessionSelector.appendChild(option);
                });

                // Restore selection if it still exists in the list
                if (currentValue) {
                    sessionSelector.value = currentValue;
                } else if (uiState.currentSessionId) {
                    sessionSelector.value = uiState.currentSessionId;
                }
            }

            function updateCurrentSessionBadge(sessionId) {
                if (sessionId) {
                    currentSessionBadge.style.display = 'block';
                    sessionIdDisplay.textContent = sessionId;
                    sessionSelector.value = sessionId;
                } else {
                    currentSessionBadge.style.display = 'none';
                    sessionSelector.value = '';
                }
            }

            function updateModelsDropdown(models) {
                // Preserve current selection before updating
                const currentValue = modelSelector.value || uiState.currentModel;

                // Group models by provider
                const providers = {};
                models.forEach(model => {
                    if (!providers[model.provider]) {
                        providers[model.provider] = [];
                    }
                    providers[model.provider].push(model);
                });

                // Clear existing options
                modelSelector.innerHTML = '';

                // Add models grouped by provider
                Object.keys(providers).sort().forEach(provider => {
                    const optgroup = document.createElement('optgroup');
                    optgroup.label = provider.charAt(0).toUpperCase() + provider.slice(1);

                    providers[provider].forEach(model => {
                        const option = document.createElement('option');
                        option.value = model.alias;

                        // Display name with special mode indicator
                        let displayName = model.alias;
                        if (model.alias.includes(':thinking')) {
                            displayName = model.alias.replace(':thinking', ' (Thinking)');
                        }

                        option.textContent = displayName;
                        optgroup.appendChild(option);
                    });

                    modelSelector.appendChild(optgroup);
                });

                // Restore selection if it exists in the list
                if (currentValue) {
                    modelSelector.value = currentValue;
                }
            }

            // Signal to extension that webview is ready to receive messages
            vscode.postMessage({ type: 'webviewReady' });

            // Focus input on load
            userInput.focus();
        `;
    }
}
