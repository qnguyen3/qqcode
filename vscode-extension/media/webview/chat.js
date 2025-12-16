// @ts-check
// QQCode Command Center - Webview Script
// Enhanced chat interface for agentic coding

(function () {
  "use strict";

  // Acquire VS Code API
  // @ts-ignore
  const vscode = acquireVsCodeApi();

  // DOM Elements
  const messagesDiv = document.getElementById("messages");
  const userInput = document.getElementById("user-input");
  const sendBtn = document.getElementById("send-btn");
  const modelSelector = document.getElementById("model-selector");
  const modelControl = document.getElementById("model-control");
  const sessionSelector = document.getElementById("session-selector");
  const newConversationBtn = document.getElementById("new-conversation-btn");
  const refreshSessionsBtn = document.getElementById("refresh-sessions-btn");
  const conversationControls = document.getElementById("conversation-controls");
  const currentSessionBadge = document.getElementById("current-session-badge");
  const sessionIdDisplay = document.getElementById("session-id-display");
  const modeSelector = document.getElementById("mode-selector");

  // State
  let currentAssistantMessage = null;
  let currentThinkingElement = null;
  let isStreaming = false;
  let toolCallMap = new Map();

  // File completion state
  let completionState = {
    suggestions: [],
    selectedIndex: 0,
    visible: false,
    requestId: 0,
    debounceTimer: null,
  };

  // UI State
  let uiState = {
    modelControlVisible: false,
    conversationControlsVisible: false,
    currentSessionId: null,
    currentModel: null,
    currentMode: "plan",
    pendingPlanApproval: null,
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
    if (uiState.modelControlVisible) {
      modelControl.style.display = "flex";
    }
    if (uiState.conversationControlsVisible) {
      conversationControls.style.display = "flex";
    }
    if (uiState.currentSessionId) {
      sessionSelector.value = uiState.currentSessionId;
      currentSessionBadge.style.display = "block";
      sessionIdDisplay.textContent = truncateId(uiState.currentSessionId);
    }
    if (uiState.currentModel) {
      modelSelector.value = uiState.currentModel;
    }
    if (uiState.currentMode) {
      modeSelector.value = uiState.currentMode;
    }
  }

  function truncateId(id) {
    if (id.length > 12) {
      return id.substring(0, 8) + "...";
    }
    return id;
  }

  // =====================
  // Event Listeners
  // =====================

  function setupEventListeners() {
    // Global keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      // Shift+Tab to cycle through modes
      if (e.shiftKey && e.key === "Tab") {
        e.preventDefault();
        cycleModeSelector();
      }
    });

    // Model selection
    modelSelector.addEventListener("change", (e) => {
      const modelAlias = e.target.value;
      if (modelAlias) {
        uiState.currentModel = modelAlias;
        saveState();
        vscode.postMessage({
          type: "selectModel",
          modelAlias: modelAlias,
        });
      }
    });

    // Mode selection
    modeSelector.addEventListener("change", (e) => {
      const mode = e.target.value;
      switchMode(mode);
    });

    // Session selection
    sessionSelector.addEventListener("change", (e) => {
      const sessionId = e.target.value;
      vscode.postMessage({
        type: "selectSession",
        sessionId: sessionId || null,
      });
    });

    // New conversation
    newConversationBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "newConversation" });
    });

    // Refresh sessions
    refreshSessionsBtn.addEventListener("click", () => {
      refreshSessionsBtn.classList.add("spinning");
      vscode.postMessage({ type: "refreshSessions" });
      setTimeout(() => refreshSessionsBtn.classList.remove("spinning"), 500);
    });

    // Send message
    sendBtn.addEventListener("click", handleSendClick);

    // Keyboard shortcuts
    userInput.addEventListener("keydown", (e) => {
      // Handle completion navigation first
      if (completionState.visible) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          moveCompletionSelection(1);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          moveCompletionSelection(-1);
          return;
        }
        if (e.key === "Tab" || e.key === "Enter") {
          if (completionState.suggestions.length > 0) {
            e.preventDefault();
            applyCompletion(
              completionState.suggestions[completionState.selectedIndex],
            );
            return;
          }
        }
        if (e.key === "Escape") {
          e.preventDefault();
          hideCompletionPopup();
          return;
        }
      }

      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-resize textarea and handle @ completions
    userInput.addEventListener("input", (e) => {
      autoResizeInput();
      handleInputChange();
    });

    // Handle blur to hide completions
    userInput.addEventListener("blur", () => {
      // Delay to allow click on completion item
      setTimeout(() => {
        if (!document.activeElement?.closest(".completion-popup")) {
          hideCompletionPopup();
        }
      }, 150);
    });

    // Handle messages from extension
    window.addEventListener("message", handleExtensionMessage);
  }

  function handleSendClick() {
    if (isStreaming) {
      cancelRequest();
    } else {
      sendMessage();
    }
  }

  function autoResizeInput() {
    userInput.style.height = "auto";
    const newHeight = Math.min(userInput.scrollHeight, 200);
    userInput.style.height = newHeight + "px";
  }

  // =====================
  // Message Handling
  // =====================

  function sendMessage() {
    const text = userInput.value.trim();
    if (!text || isStreaming) return;

    vscode.postMessage({
      type: "userMessage",
      text: text,
    });

    userInput.value = "";
    userInput.style.height = "auto";
    setStreamingState(true);
  }

  function cancelRequest() {
    vscode.postMessage({ type: "cancelRequest" });
    setStreamingState(false);
  }

  function handleExtensionMessage(event) {
    const message = event.data;

    switch (message.type) {
      case "addMessage":
        addMessage(message.role, message.content, message.isHistory || false);
        break;
      case "updateAssistantMessage":
        updateCurrentAssistantMessage(message.content);
        break;
      case "finalizeAssistantMessage":
        finalizeCurrentAssistantMessage();
        break;
      case "toolCall":
        showToolCall(message.toolName, message.toolCallId, message.args);
        break;
      case "toolResult":
        showToolResult(message.toolCallId, message.result, message.isError);
        break;
      case "historicalToolCall":
        showHistoricalToolCall(message.toolName, message.args);
        break;
      case "historicalToolResult":
        showHistoricalToolResult(message.toolName, message.isError);
        break;
      case "toolApprovalRequired":
        showToolApproval(message.toolName, message.toolCallId, message.args);
        break;
      case "planApprovalRequired":
        showPlanApproval(message.plan);
        break;
      case "planApprovalComplete":
        hidePlanApproval();
        break;
      case "thinkingUpdate":
        updateThinking(message.content);
        break;
      case "thinkingComplete":
        finalizeThinking();
        break;
      case "error":
        showError(message.message);
        setStreamingState(false);
        break;
      case "updateSessionsList":
        updateSessionsDropdown(message.sessions);
        conversationControls.style.display = "flex";
        uiState.conversationControlsVisible = true;
        saveState();
        break;
      case "updateCurrentSession":
        updateCurrentSessionBadge(message.sessionId);
        uiState.currentSessionId = message.sessionId;
        saveState();
        break;
      case "clearMessages":
        clearMessages();
        break;
      case "hideConversationControls":
        conversationControls.style.display = "none";
        uiState.conversationControlsVisible = false;
        saveState();
        break;
      case "updateModelsList":
        updateModelsDropdown(message.models);
        modelControl.style.display = "flex";
        uiState.modelControlVisible = true;
        saveState();
        break;
      case "updateCurrentModel":
        modelSelector.value = message.modelAlias || "";
        uiState.currentModel = message.modelAlias;
        saveState();
        break;
      case "hideModelControls":
        modelControl.style.display = "none";
        uiState.modelControlVisible = false;
        saveState();
        break;
      case "updateCurrentMode":
        modeSelector.value = message.mode || "plan";
        uiState.currentMode = message.mode;
        saveState();
        break;
      case "setLoading":
        setStreamingState(message.loading);
        break;
      case "fileCompletionSuggestions":
        handleFileCompletionSuggestions(message.suggestions, message.requestId);
        break;
    }
  }

  // =====================
  // UI Update Functions
  // =====================

  function setStreamingState(streaming) {
    isStreaming = streaming;
    sendBtn.innerHTML = streaming
      ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>`
      : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>`;
    sendBtn.classList.toggle("cancel", streaming);
    userInput.disabled = streaming;
  }

  function addMessage(role, content, isHistory = false) {
    const msgDiv = document.createElement("div");
    msgDiv.className = "message " + role;
    msgDiv.innerHTML = formatMessageContent(content, role);
    messagesDiv.appendChild(msgDiv);
    scrollToBottom();

    if (role === "assistant" && !isHistory) {
      currentAssistantMessage = msgDiv;
      msgDiv.classList.add("streaming");
    }
  }

  function formatMessageContent(content, role) {
    // Basic markdown-like formatting
    let formatted = escapeHtml(content);

    // Code blocks (must be before inline code)
    formatted = formatted.replace(
      /```(\w*)\n?([\s\S]*?)```/g,
      (match, lang, code) => {
        return `<pre><code class="language-${lang}">${code.trim()}</code></pre>`;
      },
    );

    // Inline code
    formatted = formatted.replace(/`([^`]+)`/g, "<code>$1</code>");

    // Bold
    formatted = formatted.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

    // Italic
    formatted = formatted.replace(/\*([^*]+)\*/g, "<em>$1</em>");

    // File mentions (for user messages)
    if (role === "user") {
      formatted = formatted.replace(
        /@([a-zA-Z0-9._/\\()\[\]{}-]+)/g,
        (match, path) => {
          return `<span class="file-mention" data-path="${escapeHtml(path)}" onclick="openFile('${escapeHtml(path)}'))">${escapeHtml(match)}</span>`;
        },
      );
    }

    // Headings (must be done line by line before converting \n to <br>)
    formatted = formatted
      .split("\n")
      .map((line) => {
        // H1
        if (line.match(/^# (.+)$/)) {
          return line.replace(/^# (.+)$/, "<h1>$1</h1>");
        }
        // H2
        if (line.match(/^## (.+)$/)) {
          return line.replace(/^## (.+)$/, "<h2>$1</h2>");
        }
        // H3
        if (line.match(/^### (.+)$/)) {
          return line.replace(/^### (.+)$/, "<h3>$1</h3>");
        }
        // H4
        if (line.match(/^#### (.+)$/)) {
          return line.replace(/^#### (.+)$/, "<h4>$1</h4>");
        }
        // Unordered list
        if (line.match(/^[\-\*] (.+)$/)) {
          return line.replace(/^[\-\*] (.+)$/, "<li>$1</li>");
        }
        // Ordered list
        if (line.match(/^\d+\. (.+)$/)) {
          return line.replace(/^\d+\. (.+)$/, "<li>$1</li>");
        }
        return line;
      })
      .join("\n");

    // Line breaks
    formatted = formatted.replace(/\n/g, "<br>");

    return formatted;
  }

  // Global function for opening files from mentions
  window.openFile = function (path) {
    // Remove trailing slash for directories
    const cleanPath = path.replace(/\/$/, "");
    vscode.postMessage({
      type: "openFile",
      path: cleanPath,
    });
  };

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function updateCurrentAssistantMessage(content) {
    if (!content) {
      return;
    }

    // Create new bubble if needed (e.g., after a tool call)
    if (!currentAssistantMessage) {
      currentAssistantMessage = document.createElement("div");
      currentAssistantMessage.className = "message assistant streaming";
      messagesDiv.appendChild(currentAssistantMessage);
    }

    // Content is already per-turn from backend, just show it directly
    currentAssistantMessage.innerHTML = formatMessageContent(
      content,
      "assistant",
    );
    scrollToBottom();
  }

  function finalizeCurrentAssistantMessage() {
    if (currentAssistantMessage) {
      currentAssistantMessage.classList.remove("streaming");
    }
    currentAssistantMessage = null;
    setStreamingState(false);
    toolCallMap.clear();
  }

  function showToolCall(toolName, toolCallId, args) {
    // Finalize current text bubble - next turn's text will be in a new bubble
    if (currentAssistantMessage) {
      currentAssistantMessage.classList.remove("streaming");
    }
    currentAssistantMessage = null;

    const toolDiv = document.createElement("div");
    toolDiv.className = "tool-call";
    toolDiv.id = `tool-${toolCallId}`;

    const header = document.createElement("div");
    header.className = "tool-call-header";
    header.textContent = formatToolName(toolName);

    const argsDiv = document.createElement("div");
    argsDiv.className = "tool-call-args";
    argsDiv.textContent = formatArgs(args);

    toolDiv.appendChild(header);
    toolDiv.appendChild(argsDiv);
    messagesDiv.appendChild(toolDiv);
    toolCallMap.set(toolCallId, toolDiv);
    scrollToBottom();
  }

  function showToolResult(toolCallId, result, isError) {
    const resultDiv = document.createElement("div");
    resultDiv.className = "tool-result" + (isError ? " error" : "");

    const header = document.createElement("div");
    header.className = "tool-result-header";
    header.innerHTML = isError
      ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> Error`
      : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg> Result`;

    const contentDiv = document.createElement("div");
    contentDiv.className = "tool-result-content";
    const truncatedResult =
      result.length > 500
        ? result.substring(0, 500) + "\n... (truncated)"
        : result;
    contentDiv.textContent = truncatedResult;

    resultDiv.appendChild(header);
    resultDiv.appendChild(contentDiv);
    messagesDiv.appendChild(resultDiv);
    scrollToBottom();
  }

  function showHistoricalToolCall(toolName, argsStr) {
    const toolDiv = document.createElement("div");
    toolDiv.className = "tool-call historical";

    const header = document.createElement("div");
    header.className = "tool-call-header";
    header.textContent = formatToolName(toolName);

    const argsDiv = document.createElement("div");
    argsDiv.className = "tool-call-args";
    argsDiv.textContent = formatHistoricalArgs(argsStr);

    toolDiv.appendChild(header);
    toolDiv.appendChild(argsDiv);
    messagesDiv.appendChild(toolDiv);
    scrollToBottom();
  }

  function showHistoricalToolResult(toolName, isError) {
    const resultDiv = document.createElement("div");
    resultDiv.className = "tool-result historical" + (isError ? " error" : "");

    const header = document.createElement("div");
    header.className = "tool-result-header";
    header.innerHTML = isError
      ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> Error`
      : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg> Done`;

    resultDiv.appendChild(header);
    messagesDiv.appendChild(resultDiv);
    scrollToBottom();
  }

  function formatHistoricalArgs(argsStr) {
    try {
      const args = JSON.parse(argsStr);
      if (typeof args === "object" && args !== null) {
        const parts = Object.entries(args)
          .slice(0, 3)
          .map(([k, v]) => {
            let vStr = String(v);
            if (vStr.length > 30) vStr = vStr.substring(0, 27) + "...";
            return `${k}=${JSON.stringify(vStr)}`;
          });
        return parts.join(", ");
      }
    } catch (e) {
      // JSON parse failed, return truncated string
    }
    return argsStr.length > 100 ? argsStr.substring(0, 97) + "..." : argsStr;
  }

  function showToolApproval(toolName, toolCallId, args) {
    const approvalDiv = document.createElement("div");
    approvalDiv.className = "tool-approval";
    approvalDiv.id = `approval-${toolCallId}`;

    const header = document.createElement("div");
    header.className = "tool-approval-header";
    header.textContent = `Approve ${formatToolName(toolName)}?`;

    const contentDiv = document.createElement("div");
    contentDiv.className = "tool-approval-content";
    contentDiv.textContent = formatArgs(args);

    const actionsDiv = document.createElement("div");
    actionsDiv.className = "tool-approval-actions";

    const approveBtn = document.createElement("button");
    approveBtn.className = "approve-btn";
    approveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Approve`;
    approveBtn.onclick = () =>
      handleToolApproval(toolCallId, true, approvalDiv);

    const rejectBtn = document.createElement("button");
    rejectBtn.className = "reject-btn";
    rejectBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Reject`;
    rejectBtn.onclick = () =>
      handleToolApproval(toolCallId, false, approvalDiv);

    actionsDiv.appendChild(approveBtn);
    actionsDiv.appendChild(rejectBtn);

    approvalDiv.appendChild(header);
    approvalDiv.appendChild(contentDiv);
    approvalDiv.appendChild(actionsDiv);
    messagesDiv.appendChild(approvalDiv);
    scrollToBottom();
  }

  function handleToolApproval(toolCallId, approved, approvalDiv) {
    vscode.postMessage({
      type: "approveToolCall",
      toolCallId: toolCallId,
      approved: approved,
    });

    const actionsDiv = approvalDiv.querySelector(".tool-approval-actions");
    if (actionsDiv) {
      actionsDiv.innerHTML = `<div class="approval-resolved ${approved ? "approved" : "rejected"}">
                ${
                  approved
                    ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Approved'
                    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Rejected'
                }
            </div>`;
    }

    // Stop the pulse animation
    approvalDiv.style.animation = "none";
  }

  function updateThinking(content) {
    if (!currentThinkingElement) {
      currentThinkingElement = document.createElement("div");
      currentThinkingElement.className = "thinking";

      const header = document.createElement("div");
      header.className = "thinking-header";
      header.innerHTML = `<span>Thinking...</span><span class="thinking-toggle"></span>`;
      header.onclick = () =>
        currentThinkingElement.classList.toggle("collapsed");

      const contentDiv = document.createElement("div");
      contentDiv.className = "thinking-content";

      currentThinkingElement.appendChild(header);
      currentThinkingElement.appendChild(contentDiv);
      messagesDiv.appendChild(currentThinkingElement);
    }

    const contentDiv =
      currentThinkingElement.querySelector(".thinking-content");
    if (contentDiv) {
      contentDiv.textContent = content;
    }
    scrollToBottom();
  }

  function finalizeThinking() {
    if (currentThinkingElement) {
      currentThinkingElement.classList.add("collapsed");
      const header = currentThinkingElement.querySelector(
        ".thinking-header span:first-child",
      );
      if (header) {
        header.textContent = "Thought process";
      }
      // Stop animation
      currentThinkingElement.style.animation = "none";
      currentThinkingElement = null;
    }
  }

  function showError(message) {
    const errorDiv = document.createElement("div");
    errorDiv.className = "error";
    errorDiv.innerHTML = `<span>${escapeHtml(message)}</span>`;
    messagesDiv.appendChild(errorDiv);
    scrollToBottom();
    currentAssistantMessage = null;
  }

  function clearMessages() {
    messagesDiv.innerHTML = "";
    currentAssistantMessage = null;
    currentThinkingElement = null;
    toolCallMap.clear();
    setStreamingState(false);
  }

  function updateSessionsDropdown(sessions) {
    const currentValue = sessionSelector.value;
    sessionSelector.innerHTML = '<option value="">New Conversation</option>';

    sessions.forEach((session) => {
      const option = document.createElement("option");
      option.value = session.session_id;

      const preview = session.last_user_message.substring(0, 35);
      const date = new Date(session.end_time);
      const timeStr = date.toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });

      option.textContent = `${preview}... (${timeStr})`;
      sessionSelector.appendChild(option);
    });

    if (currentValue) {
      sessionSelector.value = currentValue;
    } else if (uiState.currentSessionId) {
      sessionSelector.value = uiState.currentSessionId;
    }
  }

  function updateCurrentSessionBadge(sessionId) {
    if (sessionId) {
      currentSessionBadge.style.display = "block";
      sessionIdDisplay.textContent = truncateId(sessionId);
      sessionSelector.value = sessionId;
    } else {
      currentSessionBadge.style.display = "none";
      sessionSelector.value = "";
    }
  }

  function updateModelsDropdown(models) {
    const currentValue = modelSelector.value || uiState.currentModel;
    const providers = {};

    models.forEach((model) => {
      if (!providers[model.provider]) {
        providers[model.provider] = [];
      }
      providers[model.provider].push(model);
    });

    modelSelector.innerHTML = "";

    Object.keys(providers)
      .sort()
      .forEach((provider) => {
        const optgroup = document.createElement("optgroup");
        optgroup.label = provider.charAt(0).toUpperCase() + provider.slice(1);

        providers[provider].forEach((model) => {
          const option = document.createElement("option");
          option.value = model.alias;

          let displayName = model.alias;
          if (model.alias.includes(":thinking")) {
            displayName = model.alias.replace(":thinking", " (Thinking)");
          }

          option.textContent = displayName;
          optgroup.appendChild(option);
        });

        modelSelector.appendChild(optgroup);
      });

    if (currentValue) {
      modelSelector.value = currentValue;
    }
  }

  // =====================
  // Plan Approval UI
  // =====================

  function showPlanApproval(plan) {
    uiState.pendingPlanApproval = plan;
    saveState();

    const approvalDiv = document.createElement("div");
    approvalDiv.className = "plan-approval";
    approvalDiv.id = "plan-approval-container";

    approvalDiv.innerHTML = `
            <div class="plan-approval-header">Plan Ready for Review</div>
            <div class="plan-content-container">
                <div class="plan-title">Implementation Plan</div>
                <div class="plan-content">${formatMessageContent(plan, "assistant")}</div>
            </div>
            <div class="plan-options-container">
                <div class="plan-option">
                    <button class="plan-option-button" data-action="auto">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                        Execute with Auto-Approve
                    </button>
                    <div class="plan-option-description">Run the plan automatically without interruption</div>
                </div>
                <div class="plan-option">
                    <button class="plan-option-button" data-action="manual">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                        Execute with Manual Approval
                    </button>
                    <div class="plan-option-description">Review and approve each step</div>
                </div>
                <div class="plan-option">
                    <button class="plan-option-button" data-action="revise">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                        Revise Plan
                    </button>
                    <div class="plan-option-description">Provide feedback to improve the plan</div>
                </div>
            </div>
            <div class="revision-container" style="display: none;">
                <textarea class="revision-input" placeholder="Describe how you'd like to modify the plan..."></textarea>
                <div class="revision-buttons">
                    <button class="revision-cancel-btn">Cancel</button>
                    <button class="revision-submit-btn">Submit Feedback</button>
                </div>
            </div>
        `;

    // Event handlers
    approvalDiv.querySelectorAll(".plan-option-button").forEach((btn) => {
      btn.addEventListener("click", () =>
        handlePlanOptionSelect(btn.dataset.action, approvalDiv),
      );
    });

    const revisionContainer = approvalDiv.querySelector(".revision-container");
    const revisionInput = approvalDiv.querySelector(".revision-input");

    approvalDiv
      .querySelector(".revision-cancel-btn")
      .addEventListener("click", () => {
        revisionContainer.style.display = "none";
        approvalDiv.querySelector(".plan-options-container").style.display =
          "flex";
      });

    approvalDiv
      .querySelector(".revision-submit-btn")
      .addEventListener("click", () => {
        const feedback = revisionInput.value.trim();
        if (feedback) {
          sendPlanApprovalResponse(false, undefined, feedback);
        }
      });

    messagesDiv.appendChild(approvalDiv);
    scrollToBottom();

    document.addEventListener("keydown", handlePlanApprovalKeydown);
  }

  function hidePlanApproval() {
    const approvalDiv = document.getElementById("plan-approval-container");
    if (approvalDiv) {
      approvalDiv.remove();
    }
    uiState.pendingPlanApproval = null;
    saveState();
    document.removeEventListener("keydown", handlePlanApprovalKeydown);
  }

  function handlePlanOptionSelect(action, approvalDiv) {
    const optionsContainer = approvalDiv.querySelector(
      ".plan-options-container",
    );
    const revisionContainer = approvalDiv.querySelector(".revision-container");

    switch (action) {
      case "auto":
        sendPlanApprovalResponse(true, "auto-approve");
        break;
      case "manual":
        sendPlanApprovalResponse(true, "interactive");
        break;
      case "revise":
        optionsContainer.style.display = "none";
        revisionContainer.style.display = "block";
        approvalDiv.querySelector(".revision-input").focus();
        break;
    }
  }

  function sendPlanApprovalResponse(approved, mode, feedback) {
    vscode.postMessage({
      type: "planApprovalResponse",
      approved,
      mode,
      feedback,
    });
  }

  function handlePlanApprovalKeydown(event) {
    const revisionInput = document.querySelector(".revision-input");
    if (revisionInput && document.activeElement === revisionInput) {
      if (event.key === "Enter" && event.ctrlKey) {
        event.preventDefault();
        const feedback = revisionInput.value.trim();
        if (feedback) {
          sendPlanApprovalResponse(false, undefined, feedback);
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        const approvalDiv = document.getElementById("plan-approval-container");
        if (approvalDiv) {
          approvalDiv.querySelector(".revision-container").style.display =
            "none";
          approvalDiv.querySelector(".plan-options-container").style.display =
            "flex";
        }
      }
      return;
    }

    // Global shortcuts when not in revision mode
    const approvalDiv = document.getElementById("plan-approval-container");
    if (!approvalDiv) return;

    switch (event.key) {
      case "1":
        event.preventDefault();
        handlePlanOptionSelect("auto", approvalDiv);
        break;
      case "2":
        event.preventDefault();
        handlePlanOptionSelect("manual", approvalDiv);
        break;
      case "3":
      case "r":
      case "R":
        event.preventDefault();
        handlePlanOptionSelect("revise", approvalDiv);
        break;
    }
  }

  // =====================
  // File Completion
  // =====================

  function handleInputChange() {
    const text = userInput.value;
    const cursorPos = userInput.selectionStart;

    // Check if we should show completions
    if (shouldShowCompletions(text, cursorPos)) {
      requestCompletions(text, cursorPos);
    } else {
      hideCompletionPopup();
    }
  }

  function shouldShowCompletions(text, cursorPos) {
    if (cursorPos === 0) return false;

    const beforeCursor = text.substring(0, cursorPos);
    const atIndex = beforeCursor.lastIndexOf("@");

    if (atIndex === -1) return false;

    // Check if @ is at start or preceded by whitespace
    if (atIndex > 0 && !/\s/.test(beforeCursor[atIndex - 1])) {
      return false;
    }

    // Check if there's a space after @
    const fragment = beforeCursor.substring(atIndex + 1);
    return !fragment.includes(" ");
  }

  function requestCompletions(text, cursorPos) {
    // Debounce requests
    if (completionState.debounceTimer) {
      clearTimeout(completionState.debounceTimer);
    }

    completionState.debounceTimer = setTimeout(() => {
      completionState.requestId++;
      vscode.postMessage({
        type: "requestFileCompletions",
        text: text,
        cursorPosition: cursorPos,
        requestId: String(completionState.requestId),
      });
    }, 50); // 50ms debounce
  }

  function handleFileCompletionSuggestions(suggestions, requestId) {
    // Ignore stale responses
    if (parseInt(requestId) !== completionState.requestId) {
      return;
    }

    completionState.suggestions = suggestions;
    completionState.selectedIndex = 0;

    if (suggestions.length > 0) {
      showCompletionPopup();
    } else {
      hideCompletionPopup();
    }
  }

  function showCompletionPopup() {
    let popup = document.getElementById("completion-popup");

    if (!popup) {
      popup = document.createElement("div");
      popup.id = "completion-popup";
      popup.className = "completion-popup";
      document.getElementById("input-container").appendChild(popup);
    }

    const items = completionState.suggestions
      .map((suggestion, index) => {
        const isSelected = index === completionState.selectedIndex;
        const icon = suggestion.isDirectory
          ? getFolderIcon()
          : getFileIcon(suggestion.path);
        return `
                <div class="completion-item ${isSelected ? "selected" : ""}" data-index="${index}">
                    ${icon}
                    <span class="completion-label">${escapeHtml(suggestion.label)}</span>
                </div>
            `;
      })
      .join("");

    const hint = `
            <div class="completion-hint">
                <kbd>↑</kbd><kbd>↓</kbd> navigate
                <kbd>Tab</kbd> or <kbd>↵</kbd> select
                <kbd>Esc</kbd> dismiss
            </div>
        `;

    popup.innerHTML = items + hint;

    // Add click handlers
    popup.querySelectorAll(".completion-item").forEach((item) => {
      item.addEventListener("click", (e) => {
        const index = parseInt(item.dataset.index);
        applyCompletion(completionState.suggestions[index]);
      });
      item.addEventListener("mouseenter", () => {
        completionState.selectedIndex = parseInt(item.dataset.index);
        updateCompletionSelection();
      });
    });

    popup.style.display = "block";
    completionState.visible = true;
  }

  function hideCompletionPopup() {
    const popup = document.getElementById("completion-popup");
    if (popup) {
      popup.style.display = "none";
    }
    completionState.visible = false;
    completionState.suggestions = [];
    completionState.selectedIndex = 0;
  }

  function moveCompletionSelection(delta) {
    if (completionState.suggestions.length === 0) return;

    completionState.selectedIndex =
      (completionState.selectedIndex +
        delta +
        completionState.suggestions.length) %
      completionState.suggestions.length;
    updateCompletionSelection();
  }

  function updateCompletionSelection() {
    const popup = document.getElementById("completion-popup");
    if (!popup) return;

    popup.querySelectorAll(".completion-item").forEach((item, index) => {
      item.classList.toggle(
        "selected",
        index === completionState.selectedIndex,
      );
    });

    // Scroll selected item into view
    const selectedItem = popup.querySelector(".completion-item.selected");
    if (selectedItem) {
      selectedItem.scrollIntoView({ block: "nearest" });
    }
  }

  function applyCompletion(suggestion) {
    const text = userInput.value;
    const cursorPos = userInput.selectionStart;
    const beforeCursor = text.substring(0, cursorPos);
    const afterCursor = text.substring(cursorPos);

    // Find the @ position
    const atIndex = beforeCursor.lastIndexOf("@");
    if (atIndex === -1) {
      hideCompletionPopup();
      return;
    }

    // Build new text
    const prefix = text.substring(0, atIndex);
    let completion = suggestion.label;

    // Add space after completion unless it's a directory
    if (!suggestion.isDirectory) {
      completion += " ";
    }

    const newText = prefix + completion + afterCursor;
    const newCursorPos = prefix.length + completion.length;

    userInput.value = newText;
    userInput.setSelectionRange(newCursorPos, newCursorPos);
    userInput.focus();

    hideCompletionPopup();
    autoResizeInput();
  }

  function getFileIcon(path) {
    // Return empty icon for files
    return `<span class="completion-icon"></span>`;
  }

  function getFolderIcon() {
    // Return empty icon for folders
    return `<span class="completion-icon"></span>`;
  }

  // =====================
  // Mode Management
  // =====================

  function switchMode(mode) {
    uiState.currentMode = mode;
    modeSelector.value = mode;
    saveState();
    vscode.postMessage({
      type: "selectMode",
      mode: mode,
    });
    showModeNotification(mode);
  }

  function cycleModeSelector() {
    const modes = ["plan", "interactive", "auto-approve"];
    const currentIndex = modes.indexOf(uiState.currentMode || "plan");
    const nextIndex = (currentIndex + 1) % modes.length;
    const nextMode = modes[nextIndex];
    switchMode(nextMode);
  }

  function showModeNotification(mode) {
    // Remove any existing notification
    const existing = document.querySelector(".mode-notification");
    if (existing) {
      existing.remove();
    }

    // Create notification
    const notification = document.createElement("div");
    notification.className = "mode-notification";

    const modeLabels = {
      plan: "Plan Mode",
      interactive: "Interactive Mode",
      "auto-approve": "Auto-Approve Mode",
    };

    const modeIcons = {
      plan: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>',
      interactive:
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
      "auto-approve":
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
    };

    notification.innerHTML = `
            ${modeIcons[mode]}
            <span>${modeLabels[mode]}</span>
        `;

    document.body.appendChild(notification);

    // Animate in
    requestAnimationFrame(() => {
      notification.classList.add("show");
    });

    // Remove after 2 seconds
    setTimeout(() => {
      notification.classList.remove("show");
      setTimeout(() => notification.remove(), 300);
    }, 2000);
  }

  // =====================
  // Utilities
  // =====================

  function scrollToBottom() {
    const container = document.getElementById("chat-container");
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  }

  function formatToolName(toolName) {
    // Convert snake_case to Title Case
    return toolName
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  }

  function formatArgs(args) {
    try {
      const formatted = JSON.stringify(args, null, 2);
      // Truncate if too long
      if (formatted.length > 500) {
        return formatted.substring(0, 500) + "\n...";
      }
      return formatted;
    } catch {
      return String(args);
    }
  }

  // =====================
  // Initialization
  // =====================

  function init() {
    loadState();
    setupEventListeners();
    autoResizeInput();

    // Signal ready
    vscode.postMessage({ type: "webviewReady" });

    // Focus input
    userInput.focus();
  }

  init();
})();
