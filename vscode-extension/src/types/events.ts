export type QQCodeEventType =
    | 'thread.started'
    | 'item.started'
    | 'item.updated'
    | 'item.completed'
    | 'turn.started'
    | 'turn.completed'
    | 'turn.failed'
    | 'tool.call'
    | 'tool.result'
    | 'tool.approval_required'
    | 'thinking.started'
    | 'thinking.updated'
    | 'thinking.completed'
    | 'error';

export interface QQCodeEvent {
    type: QQCodeEventType;
    timestamp?: string;

    // thread.started
    thread_id?: string;
    session_id?: string;

    // item.updated
    item_id?: string;
    role?: string;
    delta?: string;
    content?: string;

    // tool.call
    tool_name?: string;
    tool_call_id?: string;
    args?: Record<string, any>;

    // tool.result
    result?: string;
    error?: string | null;
    is_error?: boolean;
    skipped?: boolean;
    skip_reason?: string | null;
    duration?: number | null;

    // turn.completed
    finish_reason?: string;
    usage?: {
        input_tokens?: number;
        output_tokens?: number;
    };

    // error
    message?: string;
    code?: string;
}

export type StreamChunk =
    | { kind: 'text'; text: string; accumulated: string }
    | { kind: 'tool_call'; toolName: string; toolCallId: string; args: any }
    | { kind: 'tool_result'; toolCallId: string; toolName: string; result: string; isError: boolean }
    | { kind: 'thinking'; text: string }
    | { kind: 'error'; message: string }
    | { kind: 'session_started'; sessionId: string };

export interface SessionSummary {
    session_id: string;
    end_time: string;
    last_user_message: string;
}

export interface SessionData {
    metadata: Record<string, any>;
    messages: Array<{
        role: string;
        content: string | null;
        reasoning_content?: string | null;
        tool_calls?: Array<any>;
        name?: string | null;
        tool_call_id?: string | null;
    }>;
}

export interface ModelInfo {
    alias: string;
    name: string;
    provider: string;
    context_limit?: number;
    input_price?: number;
    output_price?: number;
    extra_body?: Record<string, any>;
}

export interface ModelGroup {
    provider: string;
    models: ModelInfo[];
}
