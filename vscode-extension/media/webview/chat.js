// @ts-check
// QQCode Chat View - Webview Script
// This script runs inside the VS Code webview and handles UI interactions

(function() {
    'use strict';

    // Acquire VS Code API (only available in webview context)
    // @ts-ignore
    const vscode = acquireVsCodeApi();

    // DOM Elements
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
    const modeSelector = document.getElementById('mode-selector');

    // State
    let currentAssistantMessage = null;
    let currentThinkingElement = null;
    let isStreaming = false;

    // UI State for persistence across webview recreations
    let uiState = {
        modelControlsVisible: false,
        conversationControlsVisible: false,
        currentSessionId: null,
        currentModel: null,
        currentMode: 'plan',  // Default mode
        pendingPlanApproval: null
    };

    // =====================
    // State Management
    // =====================

    function loadState() {
        const previousState = vscode.getState();
        if (previousState) {
            uiState = { ...uiState, ...previousState };
            applyRestoredState();
        }
    }

    function saveState() {
        vscode.setState(uiState);
    }

    function applyRestoredState() {
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
        if (uiState.currentMode) {
            modeSelector.value = uiState.currentMode;
        }
    }

    // =====================
    // Event Listeners
    // =====================

    function setupEventListeners() {
        // Model selection
        modelSelector.addEventListener('change', (e) => {
            const modelAlias = e.target.value;
            if (modelAlias) {
                uiState.currentModel = modelAlias;
                saveState();
                vscode.postMessage({
                    type: 'selectModel',
                    modelAlias: modelAlias
                });
            }
        });

        // Mode selection
        modeSelector.addEventListener('change', (e) => {
            const mode = e.target.value;
            uiState.currentMode = mode;
            saveState();
            vscode.postMessage({
                type: 'selectMode',
                mode: mode
            });
        });

        // Session selection
        sessionSelector.addEventListener('change', (e) => {
            const sessionId = e.target.value;
            vscode.postMessage({
                type: 'selectSession',
                sessionId: sessionId || null
            });
        });

        // New conversation
        newConversationBtn.addEventListener('click', () => {
            vscode.postMessage({ type: 'newConversation' });
        });

        // Refresh sessions
        refreshSessionsBtn.addEventListener('click', () => {
            vscode.postMessage({ type: 'refreshSessions' });
        });

        // Send message
        sendBtn.addEventListener('click', sendMessage);

        // Keyboard shortcuts
        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                sendMessage();
            }
        });

        // Auto-resize textarea
        userInput.addEventListener('input', () => {
            userInput.style.height = 'auto';
            userInput.style.height = userInput.scrollHeight + 'px';
        });

        // Handle messages from extension
        window.addEventListener('message', handleExtensionMessage);
    }

    // =====================
    // Message Handling
    // =====================

    function sendMessage() {
        const text = userInput.value.trim();
        if (!text || isStreaming) return;

        vscode.postMessage({
            type: 'userMessage',
            text: text
        });

        userInput.value = '';
        userInput.style.height = 'auto';
    }

    function handleExtensionMessage(event) {
        const message = event.data;

        switch (message.type) {
            case 'addMessage':
                addMessage(message.role, message.content, message.isHistory || false);
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
            case 'toolApprovalRequired':
                showToolApproval(message.toolName, message.toolCallId, message.args);
                break;
            case 'planApprovalRequired':
                showPlanApproval(message.plan);
                break;
            case 'planApprovalComplete':
                hidePlanApproval();
                break;
            case 'thinkingUpdate':
                updateThinking(message.content);
                break;
            case 'thinkingComplete':
                finalizeThinking();
                break;
            case 'error':
                showError(message.message);
                break;
            case 'updateSessionsList':
                updateSessionsDropdown(message.sessions);
                conversationControls.style.display = 'block';
                uiState.conversationControlsVisible = true;
                saveState();
                break;
            case 'updateCurrentSession':
                updateCurrentSessionBadge(message.sessionId);
                uiState.currentSessionId = message.sessionId;
                saveState();
                break;
            case 'clearMessages':
                clearMessages();
                break;
            case 'hideConversationControls':
                conversationControls.style.display = 'none';
                uiState.conversationControlsVisible = false;
                saveState();
                showError('Session management disabled: ' + message.reason);
                break;
            case 'updateModelsList':
                updateModelsDropdown(message.models);
                modelControls.style.display = 'block';
                uiState.modelControlsVisible = true;
                saveState();
                break;
            case 'updateCurrentModel':
                modelSelector.value = message.modelAlias || '';
                uiState.currentModel = message.modelAlias;
                saveState();
                break;
            case 'hideModelControls':
                modelControls.style.display = 'none';
                uiState.modelControlsVisible = false;
                saveState();
                showError('Model loading failed: ' + message.reason);
                break;
            case 'updateCurrentMode':
                modeSelector.value = message.mode || 'plan';
                uiState.currentMode = message.mode;
                saveState();
                break;
            case 'setLoading':
                setLoadingState(message.loading);
                break;
        }
    }

    // =====================
    // UI Update Functions
    // =====================

    function addMessage(role, content, isHistory = false) {
        const msgDiv = document.createElement('div');
        msgDiv.className = 'message ' + role;
        msgDiv.textContent = content;
        messagesDiv.appendChild(msgDiv);
        scrollToBottom();

        // Only set up streaming state for non-history assistant messages
        if (role === 'assistant' && !isHistory) {
            currentAssistantMessage = msgDiv;
            msgDiv.classList.add('streaming');
            isStreaming = true;
            updateInputState();
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
            // Move the assistant message to the end so it appears after all tool calls
            messagesDiv.appendChild(currentAssistantMessage);
            scrollToBottom();
            currentAssistantMessage = null;
        }
        isStreaming = false;
        updateInputState();
    }

    function showToolCall(toolName, args) {
        const toolDiv = document.createElement('div');
        toolDiv.className = 'tool-call';

        const header = document.createElement('div');
        header.className = 'tool-call-header';
        header.textContent = '🔧 ' + toolName;

        const argsDiv = document.createElement('div');
        argsDiv.textContent = formatArgs(args);

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
        
        const truncatedResult = result.length > 500 
            ? result.substring(0, 500) + '... (truncated)' 
            : result;
        resultDiv.textContent = '→ ' + truncatedResult;
        messagesDiv.appendChild(resultDiv);
        scrollToBottom();
    }

    function showToolApproval(toolName, toolCallId, args) {
        const approvalDiv = document.createElement('div');
        approvalDiv.className = 'tool-approval';
        approvalDiv.id = `approval-${toolCallId}`;

        const header = document.createElement('div');
        header.className = 'tool-approval-header';
        header.textContent = '⚠️ Tool requires approval: ' + toolName;

        const argsDiv = document.createElement('div');
        argsDiv.textContent = formatArgs(args);

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'tool-approval-actions';

        const approveBtn = document.createElement('button');
        approveBtn.className = 'approve-btn';
        approveBtn.textContent = 'Approve';
        approveBtn.onclick = () => handleToolApproval(toolCallId, true, approvalDiv);

        const rejectBtn = document.createElement('button');
        rejectBtn.className = 'reject-btn';
        rejectBtn.textContent = 'Reject';
        rejectBtn.onclick = () => handleToolApproval(toolCallId, false, approvalDiv);

        actionsDiv.appendChild(approveBtn);
        actionsDiv.appendChild(rejectBtn);

        approvalDiv.appendChild(header);
        approvalDiv.appendChild(argsDiv);
        approvalDiv.appendChild(actionsDiv);
        messagesDiv.appendChild(approvalDiv);
        scrollToBottom();
    }

    function handleToolApproval(toolCallId, approved, approvalDiv) {
        vscode.postMessage({
            type: 'approveToolCall',
            toolCallId: toolCallId,
            approved: approved
        });

        // Update UI to show decision
        const actionsDiv = approvalDiv.querySelector('.tool-approval-actions');
        if (actionsDiv) {
            actionsDiv.innerHTML = approved 
                ? '<span style="color: var(--vscode-testing-iconPassed)">✓ Approved</span>'
                : '<span style="color: var(--vscode-errorForeground)">✗ Rejected</span>';
        }
    }

    function updateThinking(content) {
        if (!currentThinkingElement) {
            currentThinkingElement = document.createElement('div');
            currentThinkingElement.className = 'thinking';
            
            const header = document.createElement('div');
            header.className = 'thinking-header thinking-toggle';
            header.textContent = 'Thinking...';
            header.onclick = () => currentThinkingElement.classList.toggle('collapsed');

            const contentDiv = document.createElement('div');
            contentDiv.className = 'thinking-content';

            currentThinkingElement.appendChild(header);
            currentThinkingElement.appendChild(contentDiv);
            messagesDiv.appendChild(currentThinkingElement);
        }

        const contentDiv = currentThinkingElement.querySelector('.thinking-content');
        if (contentDiv) {
            contentDiv.textContent = content;
        }
        scrollToBottom();
    }

    function finalizeThinking() {
        if (currentThinkingElement) {
            // Collapse thinking by default when done
            currentThinkingElement.classList.add('collapsed');
            const header = currentThinkingElement.querySelector('.thinking-header');
            if (header) {
                header.textContent = 'Thought process (click to expand)';
            }
            currentThinkingElement = null;
        }
    }

    function showError(message) {
        const errorDiv = document.createElement('div');
        errorDiv.className = 'error';
        errorDiv.textContent = '❌ ' + message;
        messagesDiv.appendChild(errorDiv);
        scrollToBottom();

        // Reset streaming state on error
        isStreaming = false;
        updateInputState();
    }

    function clearMessages() {
        messagesDiv.innerHTML = '';
        currentAssistantMessage = null;
        currentThinkingElement = null;
        isStreaming = false;
        updateInputState();
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

    function setLoadingState(loading) {
        isStreaming = loading;
        updateInputState();
    }

    function updateInputState() {
        sendBtn.disabled = isStreaming;
        sendBtn.textContent = isStreaming ? 'Cancel' : 'Send';
        
        if (isStreaming) {
            sendBtn.onclick = () => vscode.postMessage({ type: 'cancelRequest' });
        } else {
            sendBtn.onclick = sendMessage;
        }
    }

    // =====================
    // Utilities
    // =====================

    function scrollToBottom() {
        const container = document.getElementById('chat-container');
        container.scrollTop = container.scrollHeight;
    }

    function formatArgs(args) {
        try {
            return JSON.stringify(args, null, 2);
        } catch {
            return String(args);
        }
    }

    // =====================
    // Plan Approval UI
    // =====================

    function showPlanApproval(plan) {
        // Store the plan for later use
        uiState.pendingPlanApproval = plan;
        saveState();

        // Create plan approval container
        const approvalDiv = document.createElement('div');
        approvalDiv.className = 'plan-approval';
        approvalDiv.id = 'plan-approval-container';

        // Create header
        const header = document.createElement('div');
        header.className = 'plan-approval-header';
        header.textContent = '📋 Plan Ready for Review';

        // Create plan content container
        const planContainer = document.createElement('div');
        planContainer.className = 'plan-content-container';

        // Add plan title
        const planTitle = document.createElement('div');
        planTitle.className = 'plan-title';
        planTitle.textContent = 'Review the plan below:';

        // Add plan content
        const planContent = document.createElement('div');
        planContent.className = 'plan-content';
        planContent.textContent = plan;

        // Create options container
        const optionsContainer = document.createElement('div');
        optionsContainer.className = 'plan-options-container';

        // Create option buttons
        const options = [
            { id: 'auto', text: 'Execute with Auto-Approve', description: 'Run the plan automatically' },
            { id: 'manual', text: 'Execute with Manual Approval', description: 'Review each step' },
            { id: 'revise', text: 'Revise Plan', description: 'Give feedback to improve the plan' }
        ];

        options.forEach((option, index) => {
            const optionDiv = document.createElement('div');
            optionDiv.className = 'plan-option';
            optionDiv.dataset.option = option.id;

            const optionButton = document.createElement('button');
            optionButton.className = 'plan-option-button';
            optionButton.textContent = `${index + 1}. ${option.text}`;
            optionButton.onclick = () => handlePlanOptionSelect(option.id);

            const optionDesc = document.createElement('div');
            optionDesc.className = 'plan-option-description';
            optionDesc.textContent = option.description;

            optionDiv.appendChild(optionButton);
            optionDiv.appendChild(optionDesc);
            optionsContainer.appendChild(optionDiv);
        });

        // Create revision input (hidden by default)
        const revisionContainer = document.createElement('div');
        revisionContainer.className = 'revision-container';
        revisionContainer.style.display = 'none';

        const revisionInput = document.createElement('textarea');
        revisionInput.className = 'revision-input';
        revisionInput.placeholder = 'Type your revision feedback and press Enter...';
        revisionInput.id = 'revision-input';

        const revisionButtons = document.createElement('div');
        revisionButtons.className = 'revision-buttons';

        const submitRevisionBtn = document.createElement('button');
        submitRevisionBtn.className = 'revision-submit-btn';
        submitRevisionBtn.textContent = 'Submit Revision';
        submitRevisionBtn.onclick = () => submitRevision();

        const cancelRevisionBtn = document.createElement('button');
        cancelRevisionBtn.className = 'revision-cancel-btn';
        cancelRevisionBtn.textContent = 'Cancel';
        cancelRevisionBtn.onclick = () => cancelRevision();

        revisionButtons.appendChild(submitRevisionBtn);
        revisionButtons.appendChild(cancelRevisionBtn);

        revisionContainer.appendChild(revisionInput);
        revisionContainer.appendChild(revisionButtons);

        // Assemble the approval UI
        planContainer.appendChild(planTitle);
        planContainer.appendChild(planContent);

        approvalDiv.appendChild(header);
        approvalDiv.appendChild(planContainer);
        approvalDiv.appendChild(optionsContainer);
        approvalDiv.appendChild(revisionContainer);

        // Add to messages
        messagesDiv.appendChild(approvalDiv);
        scrollToBottom();

        // Set up keyboard shortcuts
        document.addEventListener('keydown', handlePlanApprovalKeydown);
    }

    function hidePlanApproval() {
        const approvalDiv = document.getElementById('plan-approval-container');
        if (approvalDiv) {
            approvalDiv.remove();
        }

        // Clear pending plan approval
        uiState.pendingPlanApproval = null;
        saveState();

        // Remove keyboard shortcuts
        document.removeEventListener('keydown', handlePlanApprovalKeydown);
    }

    function handlePlanOptionSelect(optionId) {
        switch (optionId) {
            case 'auto':
                sendPlanApprovalResponse(true, 'auto-approve');
                break;
            case 'manual':
                sendPlanApprovalResponse(true, 'interactive');
                break;
            case 'revise':
                showRevisionInput();
                break;
        }
    }

    function showRevisionInput() {
        const optionsContainer = document.querySelector('.plan-options-container');
        const revisionContainer = document.querySelector('.revision-container');

        if (optionsContainer) {
            optionsContainer.style.display = 'none';
        }

        if (revisionContainer) {
            revisionContainer.style.display = 'block';
            document.getElementById('revision-input').focus();
        }
    }

    function cancelRevision() {
        const optionsContainer = document.querySelector('.plan-options-container');
        const revisionContainer = document.querySelector('.revision-container');

        if (optionsContainer) {
            optionsContainer.style.display = 'flex';
        }

        if (revisionContainer) {
            revisionContainer.style.display = 'none';
            document.getElementById('revision-input').value = '';
        }
    }

    function submitRevision() {
        const revisionInput = document.getElementById('revision-input');
        const feedback = revisionInput.value.trim();

        if (feedback) {
            sendPlanApprovalResponse(false, undefined, feedback);
        }
    }

    function sendPlanApprovalResponse(approved, mode, feedback) {
        vscode.postMessage({
            type: 'planApprovalResponse',
            approved,
            mode,
            feedback
        });
    }

    function handlePlanApprovalKeydown(event) {
        // Handle keyboard shortcuts for plan approval
        if (event.target.tagName === 'TEXTAREA' && event.target.id === 'revision-input') {
            // In revision input mode
            if (event.key === 'Enter' && event.ctrlKey) {
                event.preventDefault();
                submitRevision();
            } else if (event.key === 'Escape') {
                event.preventDefault();
                cancelRevision();
            }
            return;
        }

        // Global shortcuts
        switch (event.key) {
            case '1':
                event.preventDefault();
                handlePlanOptionSelect('auto');
                break;
            case '2':
                event.preventDefault();
                handlePlanOptionSelect('manual');
                break;
            case '3':
            case 'r':
            case 'R':
                event.preventDefault();
                handlePlanOptionSelect('revise');
                break;
        }
    }

    // =====================
    // Initialization
    // =====================

    function init() {
        loadState();
        setupEventListeners();
        
        // Signal to extension that webview is ready to receive messages
        vscode.postMessage({ type: 'webviewReady' });
        
        // Focus input on load
        userInput.focus();
    }

    // Start the application
    init();
})();
