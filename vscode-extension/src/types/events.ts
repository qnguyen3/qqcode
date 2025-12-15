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
    | { kind: 'error'; message: string };
