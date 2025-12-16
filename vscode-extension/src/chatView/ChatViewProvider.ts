import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { QQCodeBackend } from "../qqcodeBackend";
import { SessionSummary, ModelInfo } from "../types/events";
import { QQCodeStatusBar } from "../statusBar";
import {
  ExtensionToWebviewMessage,
  WebviewToExtensionMessage,
  ExecutionMode,
} from "./protocol/messages";
import { FileIndexer } from "../fileIndexer/FileIndexer";
import { PathCompleter } from "../fileIndexer/PathCompleter";
import { processMessage } from "./messageProcessor";

/**
 * Chat state management
 */
interface ChatState {
  currentSessionId: string | null;
  messageHistory: Array<{ role: string; content: string }>;
  currentModel: string | null;
  currentMode: ExecutionMode;
  availableSessions: SessionSummary[];
  availableModels: ModelInfo[];
  isStreaming: boolean;
  accumulatedThinking: string;
  pendingApproval: {
    toolCallId: string;
    toolName: string;
    args: unknown;
  } | null;
  pendingPlanApproval: { plan: string } | null;
}

/**
 * ChatViewProvider manages the chat webview panel in VS Code.
 *
 * This provider:
 * - Renders the chat UI using external HTML/CSS/JS assets
 * - Handles bidirectional communication with the webview
 * - Manages conversation state and persistence
 * - Streams responses from the QQCode backend
 */
export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "qqcodeChatView";

  private view?: vscode.WebviewView;
  private backend: QQCodeBackend;
  private extensionContext: vscode.ExtensionContext;
  private statusBar: QQCodeStatusBar | null;

  // State
  private state: ChatState = {
    currentSessionId: null,
    messageHistory: [],
    currentModel: null,
    currentMode: "plan",
    availableSessions: [],
    availableModels: [],
    isStreaming: false,
    accumulatedThinking: "",
    pendingApproval: null,
    pendingPlanApproval: null,
  };

  // Webview communication
  private webviewReady = false;
  private pendingMessages: ExtensionToWebviewMessage[] = [];

  // File completion
  private fileIndexer: FileIndexer;
  private pathCompleter: PathCompleter;

  constructor(
    private readonly extensionUri: vscode.Uri,
    backend: QQCodeBackend,
    extensionContext: vscode.ExtensionContext,
    statusBar?: QQCodeStatusBar | null,
  ) {
    this.backend = backend;
    this.extensionContext = extensionContext;
    this.statusBar = statusBar || null;
    this.fileIndexer = new FileIndexer();
    this.pathCompleter = new PathCompleter(this.fileIndexer);
    this.restoreState();
  }

  // =====================
  // State Persistence
  // =====================

  private saveState(): void {
    this.extensionContext.workspaceState.update(
      "qqcode.currentSessionId",
      this.state.currentSessionId,
    );
    this.extensionContext.workspaceState.update(
      "qqcode.messageHistory",
      this.state.messageHistory,
    );
  }

  private restoreState(): void {
    this.state.currentSessionId = this.extensionContext.workspaceState.get<
      string | null
    >("qqcode.currentSessionId", null);
    this.state.messageHistory = this.extensionContext.workspaceState.get<
      Array<{ role: string; content: string }>
    >("qqcode.messageHistory", []);

    const savedModel = this.extensionContext.globalState.get<string>(
      "qqcode.selectedModel",
    );
    if (savedModel) {
      this.state.currentModel = savedModel;
    }

    const savedMode = this.extensionContext.globalState.get<ExecutionMode>(
      "qqcode.selectedMode",
    );
    if (savedMode) {
      this.state.currentMode = savedMode;
    }
  }

  // =====================
  // Webview Lifecycle
  // =====================

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this.view = webviewView;
    this.webviewReady = false;
    this.pendingMessages = [];

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.extensionUri, "media", "webview"),
      ],
    };

    webviewView.webview.html = this.getHtmlContent(webviewView.webview);

    // Handle messages from webview
    webviewView.webview.onDidReceiveMessage((data: WebviewToExtensionMessage) =>
      this.handleWebviewMessage(data),
    );

    // Refresh data when view becomes visible
    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible && this.webviewReady) {
        this.refreshSessionsList();
        this.refreshModelsList();
      }
    });

    // Handle disposal
    webviewView.onDidDispose(() => {
      this.webviewReady = false;
      this.view = undefined;
    });
  }

  // =====================
  // Message Handling
  // =====================

  private async handleWebviewMessage(
    message: WebviewToExtensionMessage,
  ): Promise<void> {
    switch (message.type) {
      case "webviewReady":
        this.webviewReady = true;
        this.flushPendingMessages();
        await this.initializeWebview();
        break;

      case "userMessage":
        await this.handleUserMessage(message.text);
        break;

      case "cancelRequest":
        this.backend.cancel();
        this.state.isStreaming = false;
        this.sendToWebview({ type: "setLoading", loading: false });
        break;

      case "newConversation":
        await this.switchToSession(null);
        break;

      case "selectSession":
        await this.switchToSession(message.sessionId);
        break;

      case "refreshSessions":
        await this.refreshSessionsList();
        break;

      case "selectModel":
        await this.switchModel(message.modelAlias);
        break;

      case "selectMode":
        this.switchMode(message.mode);
        break;

      case "approveToolCall":
        // Send approval response to backend via stdin
        this.backend.sendApprovalResponse(
          message.toolCallId,
          message.approved,
          message.approved ? undefined : "User rejected the tool call",
        );
        this.state.pendingApproval = null;
        break;

      case "planApprovalResponse":
        // Handle plan approval response
        this.handlePlanApprovalResponse(
          message.approved,
          message.mode,
          message.feedback,
        );
        this.state.pendingPlanApproval = null;
        break;

      case "requestFileCompletions":
        await this.handleFileCompletionRequest(
          message.text,
          message.cursorPosition,
          message.requestId,
        );
        break;

      case "openFile":
        await this.handleOpenFile(message.path);
        break;
    }
  }

  // =====================
  // File Completion
  // =====================

  private async handleFileCompletionRequest(
    text: string,
    cursorPosition: number,
    requestId: string,
  ): Promise<void> {
    const workspaceRoot = this.getWorkspaceRoot();
    if (!workspaceRoot) {
      this.sendToWebview({
        type: "fileCompletionSuggestions",
        suggestions: [],
        requestId,
      });
      return;
    }

    try {
      const suggestions = await this.pathCompleter.getCompletions(
        text,
        cursorPosition,
        workspaceRoot,
      );

      this.sendToWebview({
        type: "fileCompletionSuggestions",
        suggestions,
        requestId,
      });
    } catch (error) {
      console.error("Failed to get file completions:", error);
      this.sendToWebview({
        type: "fileCompletionSuggestions",
        suggestions: [],
        requestId,
      });
    }
  }

  private async handleOpenFile(filePath: string): Promise<void> {
    const workspaceRoot = this.getWorkspaceRoot();
    if (!workspaceRoot) {
      return;
    }

    try {
      const fileUri = vscode.Uri.joinPath(workspaceRoot, filePath);
      const document = await vscode.workspace.openTextDocument(fileUri);
      await vscode.window.showTextDocument(document);
    } catch (error) {
      console.error("Failed to open file:", error);
    }
  }

  private getWorkspaceRoot(): vscode.Uri | null {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      return null;
    }
    return workspaceFolders[0].uri;
  }

  private async initializeWebview(): Promise<void> {
    // Load sessions and models in parallel
    await Promise.all([this.refreshSessionsList(), this.refreshModelsList()]);

    // Restore conversation state
    if (this.state.currentSessionId && this.state.messageHistory.length > 0) {
      for (const msg of this.state.messageHistory) {
        this.sendToWebview({
          type: "addMessage",
          role: msg.role as "user" | "assistant",
          content: msg.content,
          isHistory: true,
        });
      }

      this.sendToWebview({
        type: "updateCurrentSession",
        sessionId: this.state.currentSessionId,
      });
    }

    // Restore model selection
    if (this.state.currentModel) {
      this.sendToWebview({
        type: "updateCurrentModel",
        modelAlias: this.state.currentModel,
      });
    }

    // Restore mode selection
    this.sendToWebview({
      type: "updateCurrentMode",
      mode: this.state.currentMode,
    });
  }

  // =====================
  // User Message Handling
  // =====================

  private async handleUserMessage(text: string): Promise<void> {
    // Prevent duplicate requests
    if (this.state.isStreaming) {
      return;
    }

    this.state.isStreaming = true;
    this.state.accumulatedThinking = "";
    this.statusBar?.setThinking();

    // Process @ mentions and embed file contents
    const workspaceRoot = this.getWorkspaceRoot();
    let processedText = text;
    let displayText = text;

    if (workspaceRoot) {
      try {
        const processed = await processMessage(text, workspaceRoot);
        processedText = processed.promptText;
        displayText = processed.displayText;

        // Log mentions for debugging
        if (processed.mentions.length > 0) {
          console.log(
            `[QQCode] Processed ${processed.mentions.length} file mention(s)`,
          );
        }
      } catch (error) {
        console.error("Failed to process message:", error);
        // Fall back to original text
      }
    }

    // Add user message to UI (show display text, not processed text)
    this.sendToWebview({
      type: "addMessage",
      role: "user",
      content: displayText,
    });
    this.state.messageHistory.push({ role: "user", content: displayText });

    const requestedSessionId = this.state.currentSessionId;
    let assistantMessage = "";

    try {
      // Send processed text (with embedded file contents) to backend
      for await (const chunk of this.backend.streamPrompt(
        processedText,
        this.state.currentMode,
        this.state.currentSessionId || undefined,
        this.state.currentModel || undefined,
      )) {
        switch (chunk.kind) {
          case "session_started":
            this.handleSessionStarted(chunk.sessionId, requestedSessionId);
            break;

          case "text":
            assistantMessage += chunk.text;
            this.sendToWebview({
              type: "updateAssistantMessage",
              content: chunk.accumulated,
            });
            this.statusBar?.setStreaming();
            break;

          case "tool_call":
            this.sendToWebview({
              type: "toolCall",
              toolName: chunk.toolName,
              toolCallId: chunk.toolCallId,
              args: chunk.args,
            });
            this.statusBar?.setRunningTool(chunk.toolName);
            break;

          case "tool_result":
            this.sendToWebview({
              type: "toolResult",
              toolCallId: chunk.toolCallId,
              result: chunk.result,
              isError: chunk.isError,
            });
            break;

          case "tool_approval_required":
            // Store pending approval and show UI
            this.state.pendingApproval = {
              toolCallId: chunk.toolCallId,
              toolName: chunk.toolName,
              args: chunk.args,
            };
            this.sendToWebview({
              type: "toolApprovalRequired",
              toolName: chunk.toolName,
              toolCallId: chunk.toolCallId,
              args: chunk.args as Record<string, unknown>,
            });
            this.statusBar?.setWaitingApproval(chunk.toolName);
            break;

          case "plan_approval_required":
            // Store pending plan approval and show UI
            this.state.pendingPlanApproval = {
              plan: chunk.plan || "",
            };
            this.sendToWebview({
              type: "planApprovalRequired",
              plan: chunk.plan || "",
            });
            this.statusBar?.setPlanReview();
            break;

          case "thinking":
            this.state.accumulatedThinking += chunk.text;
            this.sendToWebview({
              type: "thinkingUpdate",
              content: this.state.accumulatedThinking,
            });
            this.statusBar?.setThinking();
            break;

          case "error":
            this.sendToWebview({
              type: "error",
              message: chunk.message,
            });
            this.statusBar?.setError(chunk.message);
            break;
        }
      }

      // Finalize thinking if we had any
      if (this.state.accumulatedThinking) {
        this.sendToWebview({ type: "thinkingComplete" });
      }

      // Finalize assistant message
      this.state.messageHistory.push({
        role: "assistant",
        content: assistantMessage,
      });
      this.sendToWebview({ type: "finalizeAssistantMessage" });
      this.saveState();

      // Refresh sessions list
      await this.refreshSessionsList();
    } catch (error) {
      this.sendToWebview({
        type: "error",
        message: `Error: ${error}`,
      });
      this.statusBar?.setError(String(error));
    } finally {
      this.state.isStreaming = false;
      this.state.accumulatedThinking = "";
      this.statusBar?.setReady();
    }
  }

  private handleSessionStarted(
    returnedSessionId: string,
    requestedSessionId: string | null,
  ): void {
    if (!this.state.currentSessionId) {
      this.state.currentSessionId = returnedSessionId;
      console.log(
        `[QQCode] New session started: ${this.state.currentSessionId}`,
      );
    } else if (requestedSessionId && returnedSessionId !== requestedSessionId) {
      console.warn(
        `[QQCode] Session ID mismatch! Requested: ${requestedSessionId}, Got: ${returnedSessionId}`,
      );
      this.state.currentSessionId = returnedSessionId;
    } else {
      console.log(
        `[QQCode] Continuing session: ${this.state.currentSessionId}`,
      );
    }

    this.sendToWebview({
      type: "updateCurrentSession",
      sessionId: this.state.currentSessionId,
    });
  }

  private handlePlanApprovalResponse(
    approved: boolean,
    mode?: ExecutionMode,
    feedback?: string,
  ): void {
    // Send plan approval response to backend
    this.backend.sendPlanApprovalResponse(approved, mode, feedback);

    // Update mode if approval includes a mode change
    if (approved && mode) {
      this.switchMode(mode);
    }

    // Notify the webview that the plan approval is complete
    this.sendToWebview({
      type: "planApprovalComplete",
      approved,
      mode,
      feedback,
    });
  }

  // =====================
  // Session Management
  // =====================

  private async refreshSessionsList(): Promise<void> {
    try {
      this.state.availableSessions = await this.backend.listSessions();
      this.sendToWebview({
        type: "updateSessionsList",
        sessions: this.state.availableSessions,
      });
    } catch (error) {
      console.error("Failed to load sessions:", error);
      this.sendToWebview({
        type: "hideConversationControls",
        reason: error instanceof Error ? error.message : "Unknown error",
      });
    }
  }

  private async switchToSession(sessionId: string | null): Promise<void> {
    if (sessionId === this.state.currentSessionId) {
      return;
    }

    this.state.currentSessionId = sessionId;
    this.sendToWebview({ type: "clearMessages" });
    this.state.messageHistory = [];

    if (sessionId) {
      try {
        const sessionData = await this.backend.getSession(sessionId);
        if (sessionData) {
          for (const msg of sessionData.messages) {
            if (msg.role === "user" && msg.content) {
              // User messages
              this.sendToWebview({
                type: "addMessage",
                role: "user",
                content: msg.content,
                isHistory: true,
              });
              this.state.messageHistory.push({
                role: "user",
                content: msg.content,
              });
            } else if (msg.role === "assistant") {
              // Assistant content
              if (msg.content) {
                this.sendToWebview({
                  type: "addMessage",
                  role: "assistant",
                  content: msg.content,
                  isHistory: true,
                });
                this.state.messageHistory.push({
                  role: "assistant",
                  content: msg.content,
                });
              }
              // Tool calls from this assistant message
              if (msg.tool_calls && msg.tool_calls.length > 0) {
                for (const toolCall of msg.tool_calls) {
                  if (toolCall.function?.name) {
                    this.sendToWebview({
                      type: "historicalToolCall",
                      toolName: toolCall.function.name,
                      args: toolCall.function.arguments || "{}",
                    });
                  }
                }
              }
            } else if (msg.role === "tool") {
              // Tool results
              const contentStr = msg.content || "";
              const isError = Boolean(
                contentStr &&
                  (contentStr
                    .toLowerCase()
                    .substring(0, 50)
                    .includes("error") ||
                    contentStr.startsWith("Error:")),
              );
              this.sendToWebview({
                type: "historicalToolResult",
                toolName: msg.name || "tool",
                isError: isError,
              });
            }
          }
        }
      } catch (error) {
        console.error(`[QQCode] Failed to load session ${sessionId}:`, error);
        this.sendToWebview({
          type: "error",
          message: `Failed to load conversation: ${error}`,
        });
      }
    }

    this.sendToWebview({
      type: "updateCurrentSession",
      sessionId: this.state.currentSessionId,
    });

    this.saveState();
  }

  // =====================
  // Model Management
  // =====================

  private async refreshModelsList(): Promise<void> {
    try {
      this.state.availableModels = await this.backend.listModels();
      this.sendToWebview({
        type: "updateModelsList",
        models: this.state.availableModels,
      });

      this.state.currentModel = await this.backend.getCurrentModel();
      this.sendToWebview({
        type: "updateCurrentModel",
        modelAlias: this.state.currentModel,
      });
    } catch (error) {
      console.error("Failed to load models:", error);
      this.sendToWebview({
        type: "hideModelControls",
        reason: error instanceof Error ? error.message : "Unknown error",
      });
    }
  }

  private async switchModel(modelAlias: string): Promise<void> {
    if (modelAlias === this.state.currentModel) {
      return;
    }

    this.state.currentModel = modelAlias;
    this.extensionContext.globalState.update(
      "qqcode.selectedModel",
      modelAlias,
    );

    this.sendToWebview({
      type: "updateCurrentModel",
      modelAlias: modelAlias,
    });

    console.log(`[QQCode] Switched to model: ${modelAlias}`);
  }

  // =====================
  // Mode Management
  // =====================

  private switchMode(mode: ExecutionMode): void {
    if (mode === this.state.currentMode) {
      return;
    }

    this.state.currentMode = mode;
    this.extensionContext.globalState.update("qqcode.selectedMode", mode);

    this.sendToWebview({
      type: "updateCurrentMode",
      mode: mode,
    });

    console.log(`[QQCode] Switched to mode: ${mode}`);
  }

  // =====================
  // Webview Communication
  // =====================

  private sendToWebview(message: ExtensionToWebviewMessage): void {
    if (this.webviewReady && this.view) {
      this.view.webview.postMessage(message);
    } else {
      this.pendingMessages.push(message);
    }
  }

  private flushPendingMessages(): void {
    for (const msg of this.pendingMessages) {
      this.view?.webview.postMessage(msg);
    }
    this.pendingMessages = [];
  }

  // =====================
  // HTML Content Generation
  // =====================

  private getHtmlContent(webview: vscode.Webview): string {
    const webviewPath = path.join(this.extensionUri.fsPath, "media", "webview");

    // Get URIs for CSS and JS files
    const styleUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(webviewPath, "styles.css")),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.file(path.join(webviewPath, "chat.js")),
    );

    // Read HTML template
    const htmlPath = path.join(webviewPath, "index.html");
    let html = fs.readFileSync(htmlPath, "utf-8");

    // Replace placeholders
    html = html
      .replace(/\{\{cspSource\}\}/g, webview.cspSource)
      .replace(/\{\{styleUri\}\}/g, styleUri.toString())
      .replace(/\{\{scriptUri\}\}/g, scriptUri.toString());

    return html;
  }
}
