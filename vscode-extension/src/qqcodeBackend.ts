import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { QQCodeEvent, StreamChunk } from './types/events';

export class QQCodeBackend {
    private commandPath: string;
    private workspaceRoot: string;
    private currentProcess: ChildProcess | null = null;
    private outputChannel: vscode.OutputChannel;

    constructor(
        commandPath: string,
        workspaceRoot: string,
        outputChannel: vscode.OutputChannel
    ) {
        this.commandPath = commandPath;
        this.workspaceRoot = workspaceRoot;
        this.outputChannel = outputChannel;
    }

    /**
     * Stream prompt to QQCode CLI and yield parsed events
     */
    async *streamPrompt(
        prompt: string,
        autoApprove: boolean = false
    ): AsyncGenerator<StreamChunk> {
        const args = [
            '--prompt', prompt,
            '--output', 'vscode'
        ];

        if (autoApprove) {
            args.push('--auto-approve');
        }

        // Build context file
        const contextPath = this.buildContextFile();

        // Determine working directory for the command
        // If using 'uv run qqcode', we need to be in the qqcode project directory
        let cwd = this.workspaceRoot;
        if (this.commandPath.includes('uv run')) {
            // Check if we're in the qqcode directory
            const fs = require('fs');
            const path = require('path');
            if (fs.existsSync(path.join(this.workspaceRoot, 'pyproject.toml'))) {
                cwd = this.workspaceRoot;
            } else {
                this.outputChannel.appendLine('[Warning] Using "uv run qqcode" but not in qqcode project directory');
                this.outputChannel.appendLine('[Warning] Command may fail. Set qqcode.commandPath to absolute path or install qqcode globally');
            }
        }

        const spawnOptions = {
            cwd: cwd,
            env: {
                ...process.env,
                QQCODE_VSCODE_CONTEXT: contextPath
            }
        };

        this.outputChannel.appendLine(`[QQCode] Spawning: ${this.commandPath} ${args.join(' ')}`);
        this.currentProcess = spawn(this.commandPath, args, spawnOptions);

        if (!this.currentProcess.stdout) {
            throw new Error('Failed to spawn QQCode process');
        }

        // Parse line-delimited JSON events
        const rl = readline.createInterface({
            input: this.currentProcess.stdout,
            crlfDelay: Infinity
        });

        try {
            for await (const line of rl) {
                try {
                    const event: QQCodeEvent = JSON.parse(line);
                    this.outputChannel.appendLine(`[Event] ${event.type}`);

                    const chunk = this.parseEvent(event);
                    if (chunk) {
                        yield chunk;
                    }
                } catch (error) {
                    this.outputChannel.appendLine(`[Error] Failed to parse event: ${line}`);
                    this.outputChannel.appendLine(`[Error] ${error}`);
                }
            }
        } finally {
            // Clean up context file
            if (contextPath && fs.existsSync(contextPath)) {
                fs.unlinkSync(contextPath);
            }
        }

        // Handle stderr
        if (this.currentProcess.stderr) {
            this.currentProcess.stderr.on('data', (data) => {
                this.outputChannel.appendLine(`[QQCode stderr] ${data}`);
            });
        }

        // Wait for process to exit
        await new Promise<void>((resolve, reject) => {
            this.currentProcess?.on('exit', (code) => {
                this.outputChannel.appendLine(`[QQCode] Process exited with code ${code}`);
                if (code === 0) {
                    resolve();
                } else {
                    reject(new Error(`QQCode exited with code ${code}`));
                }
            });

            this.currentProcess?.on('error', (error) => {
                this.outputChannel.appendLine(`[QQCode] Process error: ${error}`);
                reject(error);
            });
        });
    }

    /**
     * Convert QQCode JSON event to UI-friendly chunk
     */
    private parseEvent(event: QQCodeEvent): StreamChunk | null {
        switch (event.type) {
            case 'item.updated':
                return {
                    kind: 'text',
                    text: event.delta || '',
                    accumulated: event.content || ''
                };

            case 'tool.call':
                return {
                    kind: 'tool_call',
                    toolName: event.tool_name!,
                    toolCallId: event.tool_call_id!,
                    args: event.args || {}
                };

            case 'tool.result':
                return {
                    kind: 'tool_result',
                    toolCallId: event.tool_call_id!,
                    toolName: event.tool_name!,
                    result: event.result || '',
                    isError: event.is_error || false
                };

            case 'thinking.updated':
                return {
                    kind: 'thinking',
                    text: event.delta || ''
                };

            case 'error':
                return {
                    kind: 'error',
                    message: event.message || 'Unknown error'
                };

            // Metadata events - log but don't yield
            case 'thread.started':
            case 'turn.started':
            case 'turn.completed':
            case 'item.completed':
                return null;

            default:
                return null;
        }
    }

    /**
     * Build VSCode context and write to temp file
     */
    private buildContextFile(): string {
        const activeEditor = vscode.window.activeTextEditor;
        if (!activeEditor) {
            return '';
        }

        const contextPath = path.join(os.tmpdir(), `qqcode-context-${Date.now()}.md`);
        const context = this.buildContext(activeEditor);

        fs.writeFileSync(contextPath, context, 'utf-8');
        return contextPath;
    }

    private buildContext(editor: vscode.TextEditor): string {
        const doc = editor.document;
        const selection = editor.selection;

        let context = `## VS Code Context\n\n`;
        context += `### Active File\n`;
        context += `Path: ${doc.uri.fsPath}\n`;
        context += `Language: ${doc.languageId}\n\n`;

        if (!selection.isEmpty) {
            const selectedText = doc.getText(selection);
            context += `### Selected Code\n\`\`\`${doc.languageId}\n${selectedText}\n\`\`\`\n`;
        }

        return context;
    }

    /**
     * Cancel current running process
     */
    cancel(): void {
        if (this.currentProcess) {
            this.outputChannel.appendLine('[QQCode] Cancelling process');
            this.currentProcess.kill();
            this.currentProcess = null;
        }
    }
}
