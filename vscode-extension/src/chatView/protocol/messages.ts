/**
 * Typed message protocol for communication between the VS Code extension and the webview.
 * This provides type safety for all messages passed between the two contexts.
 */

import { SessionSummary, ModelInfo } from "../../types/events";

// Execution mode type
export type ExecutionMode = "plan" | "interactive" | "auto-approve";

// =====================
// Extension → Webview Messages
// =====================

export type AddMessagePayload = {
  type: "addMessage";
  role: "user" | "assistant";
  content: string;
  isHistory?: boolean;
};

export type UpdateAssistantMessagePayload = {
  type: "updateAssistantMessage";
  content: string;
};

export type FinalizeAssistantMessagePayload = {
  type: "finalizeAssistantMessage";
};

export type ToolCallPayload = {
  type: "toolCall";
  toolName: string;
  toolCallId: string;
  args: Record<string, unknown>;
};

export type ToolResultPayload = {
  type: "toolResult";
  toolCallId: string;
  result: string;
  isError: boolean;
};

export type ToolApprovalRequiredPayload = {
  type: "toolApprovalRequired";
  toolName: string;
  toolCallId: string;
  args: Record<string, unknown>;
};

export type ThinkingUpdatePayload = {
  type: "thinkingUpdate";
  content: string;
};

export type ThinkingCompletePayload = {
  type: "thinkingComplete";
};

export type ErrorPayload = {
  type: "error";
  message: string;
};

export type UpdateSessionsListPayload = {
  type: "updateSessionsList";
  sessions: SessionSummary[];
};

export type UpdateCurrentSessionPayload = {
  type: "updateCurrentSession";
  sessionId: string | null;
};

export type ClearMessagesPayload = {
  type: "clearMessages";
};

export type HideConversationControlsPayload = {
  type: "hideConversationControls";
  reason: string;
};

export type UpdateModelsListPayload = {
  type: "updateModelsList";
  models: ModelInfo[];
};

export type UpdateCurrentModelPayload = {
  type: "updateCurrentModel";
  modelAlias: string | null;
};

export type HideModelControlsPayload = {
  type: "hideModelControls";
  reason: string;
};

export type SetLoadingPayload = {
  type: "setLoading";
  loading: boolean;
};

export type UpdateCurrentModePayload = {
  type: "updateCurrentMode";
  mode: ExecutionMode;
};

export type PlanApprovalRequiredPayload = {
  type: "planApprovalRequired";
  plan: string;
};

export type PlanApprovalCompletePayload = {
  type: "planApprovalComplete";
  approved: boolean;
  mode?: ExecutionMode;
  feedback?: string;
};

export type HistoricalToolCallPayload = {
  type: "historicalToolCall";
  toolName: string;
  toolCallId: string;
  args: string; // JSON string of args for display
};

export type HistoricalToolResultPayload = {
  type: "historicalToolResult";
  toolCallId: string;
  toolName: string;
  isError: boolean;
};

export type FileCompletionSuggestion = {
  label: string;
  path: string;
  isDirectory: boolean;
};

export type FileCompletionSuggestionsPayload = {
  type: "fileCompletionSuggestions";
  suggestions: FileCompletionSuggestion[];
  requestId: string;
};

/**
 * All possible messages sent from extension to webview
 */
export type ExtensionToWebviewMessage =
  | AddMessagePayload
  | UpdateAssistantMessagePayload
  | FinalizeAssistantMessagePayload
  | ToolCallPayload
  | ToolResultPayload
  | ToolApprovalRequiredPayload
  | ThinkingUpdatePayload
  | ThinkingCompletePayload
  | ErrorPayload
  | UpdateSessionsListPayload
  | UpdateCurrentSessionPayload
  | ClearMessagesPayload
  | HideConversationControlsPayload
  | UpdateModelsListPayload
  | UpdateCurrentModelPayload
  | HideModelControlsPayload
  | SetLoadingPayload
  | UpdateCurrentModePayload
  | PlanApprovalRequiredPayload
  | PlanApprovalCompletePayload
  | FileCompletionSuggestionsPayload
  | HistoricalToolCallPayload
  | HistoricalToolResultPayload;

// =====================
// Webview → Extension Messages
// =====================

export type WebviewReadyMessage = {
  type: "webviewReady";
};

export type UserMessageMessage = {
  type: "userMessage";
  text: string;
};

export type CancelRequestMessage = {
  type: "cancelRequest";
};

export type NewConversationMessage = {
  type: "newConversation";
};

export type SelectSessionMessage = {
  type: "selectSession";
  sessionId: string | null;
};

export type RefreshSessionsMessage = {
  type: "refreshSessions";
};

export type SelectModelMessage = {
  type: "selectModel";
  modelAlias: string;
};

export type SelectModeMessage = {
  type: "selectMode";
  mode: ExecutionMode;
};

export type ApproveToolCallMessage = {
  type: "approveToolCall";
  toolCallId: string;
  approved: boolean;
};

export type PlanApprovalResponseMessage = {
  type: "planApprovalResponse";
  approved: boolean;
  mode?: ExecutionMode;
  feedback?: string;
};

export type RequestFileCompletionsMessage = {
  type: "requestFileCompletions";
  text: string;
  cursorPosition: number;
  requestId: string;
};

export type OpenFileMessage = {
  type: "openFile";
  path: string;
};

/**
 * All possible messages sent from webview to extension
 */
export type WebviewToExtensionMessage =
  | WebviewReadyMessage
  | UserMessageMessage
  | CancelRequestMessage
  | NewConversationMessage
  | SelectSessionMessage
  | RefreshSessionsMessage
  | SelectModelMessage
  | SelectModeMessage
  | ApproveToolCallMessage
  | PlanApprovalResponseMessage
  | RequestFileCompletionsMessage
  | OpenFileMessage;

// =====================
// Type Guards
// =====================

export function isExtensionMessage(
  msg: unknown,
): msg is ExtensionToWebviewMessage {
  return typeof msg === "object" && msg !== null && "type" in msg;
}

export function isWebviewMessage(
  msg: unknown,
): msg is WebviewToExtensionMessage {
  return typeof msg === "object" && msg !== null && "type" in msg;
}

// =====================
// Message Type Mapping
// =====================

/**
 * Extract the message type string from a message type
 */
export type ExtractMessageType<T extends { type: string }> = T["type"];

/**
 * Map of message type strings to their full message types (Extension → Webview)
 */
export type ExtensionMessageMap = {
  [K in ExtensionToWebviewMessage as K["type"]]: K;
};

/**
 * Map of message type strings to their full message types (Webview → Extension)
 */
export type WebviewMessageMap = {
  [K in WebviewToExtensionMessage as K["type"]]: K;
};
