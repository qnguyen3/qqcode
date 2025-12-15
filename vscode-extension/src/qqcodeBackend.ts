import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { QQCodeEvent, StreamChunk, SessionSummary, SessionData, ModelInfo } from './types/events';

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
     * Helper to run a non-streaming command and capture output
     */
    private async runCommand(args: string[]): Promise<string> {
        return new Promise((resolve, reject) => {
            // Determine working directory for the command
            let cwd = this.workspaceRoot;
            if (this.commandPath.includes('uv run')) {
                if (fs.existsSync(path.join(this.workspaceRoot, 'pyproject.toml'))) {
                    cwd = this.workspaceRoot;
                } else {
                    this.outputChannel.appendLine('[Warning] Using "uv run qqcode" but not in qqcode project directory');
                }
            }

            // Parse command and build args
            let command: string;
            let commandArgs: string[];

            if (this.commandPath === 'uv run qqcode') {
                command = 'uv';
                commandArgs = ['run', 'qqcode', ...args];
            } else if (this.commandPath.startsWith('uv run ')) {
                const parts = this.commandPath.split(' ');
                command = parts[0];
                commandArgs = parts.slice(1).concat(args);
            } else {
                command = this.commandPath;
                commandArgs = args;
            }

            const spawnOptions = {
                cwd: cwd,
                env: process.env,
            };

            this.outputChannel.appendLine(`[QQCode] Running command: ${command} ${commandArgs.join(' ')}`);

            const proc = spawn(command, commandArgs, spawnOptions);
            let output = '';
            let errorOutput = '';

            proc.stdout?.on('data', (data) => {
                output += data.toString();
            });

            proc.stderr?.on('data', (data) => {
                const text = data.toString();
                errorOutput += text;
                this.outputChannel.appendLine(`[QQCode stderr] ${text}`);
            });

            proc.on('exit', (code) => {
                if (code === 0) {
                    resolve(output);
                } else {
                    const error = errorOutput || `Command exited with code ${code}`;
                    reject(new Error(error));
                }
            });

            proc.on('error', (error) => {
                reject(error);
            });
        });
    }

    /**
     * List available sessions
     */
    async listSessions(): Promise<SessionSummary[]> {
        try {
            const result = await this.runCommand(['--list-sessions']);
            return JSON.parse(result);
        } catch (error) {
            this.outputChannel.appendLine(`[QQCode] Failed to list sessions: ${error}`);
            throw error;
        }
    }

    /**
     * Get session data by ID
     */
    async getSession(sessionId: string): Promise<SessionData | null> {
        try {
            const result = await this.runCommand(['--get-session', sessionId]);
            return JSON.parse(result);
        } catch (error) {
            this.outputChannel.appendLine(`[QQCode] Failed to get session: ${error}`);
            return null;
        }
    }

    /**
     * Get current active model
     */
    async getCurrentModel(): Promise<string> {
        try {
            const result = await this.runCommand(['--get-model']);
            const data = JSON.parse(result);
            return data.current_model || '';
        } catch (error) {
            this.outputChannel.appendLine(`[QQCode] Failed to get current model: ${error}`);
            throw error;
        }
    }

    /**
     * List all available models
     */
    async listModels(): Promise<ModelInfo[]> {
        try {
            const result = await this.runCommand(['--list-models']);
            const data = JSON.parse(result);
            return data.models || [];
        } catch (error) {
            this.outputChannel.appendLine(`[QQCode] Failed to list models: ${error}`);
            throw error;
        }
    }

    /**
     * Stream prompt to QQCode CLI and yield parsed events
     */
    async *streamPrompt(
        prompt: string,
        autoApprove: boolean = false,
        sessionId?: string,
        modelAlias?: string
    ): AsyncGenerator<StreamChunk> {
        // Build context file
        const contextPath = this.buildContextFile();

        // Determine working directory for the command
        let cwd = this.workspaceRoot;
        if (this.commandPath.includes('uv run')) {
            if (fs.existsSync(path.join(this.workspaceRoot, 'pyproject.toml'))) {
                cwd = this.workspaceRoot;
            } else {
                this.outputChannel.appendLine('[Warning] Using "uv run qqcode" but not in qqcode project directory');
                this.outputChannel.appendLine('[Warning] Command may fail. Set qqcode.commandPath to absolute path or install qqcode globally');
            }
        }

        // Parse command and build args
        let command: string;
        let args: string[];

        if (this.commandPath === 'uv run qqcode') {
            command = 'uv';
            args = ['run', 'qqcode', '--prompt', prompt, '--output', 'vscode'];
        } else if (this.commandPath.startsWith('uv run ')) {
            const parts = this.commandPath.split(' ');
            command = parts[0];
            args = parts.slice(1);
            args.push('--prompt', prompt, '--output', 'vscode');
        } else {
            command = this.commandPath;
            args = ['--prompt', prompt, '--output', 'vscode'];
        }

        // Add session ID if provided
        if (sessionId) {
            args.push('--resume', sessionId);
        }

        // Add model alias if provided
        if (modelAlias) {
            args.push('--model', modelAlias);
        }

        if (autoApprove) {
            args.push('--auto-approve');
        }

        const spawnOptions = {
            cwd: cwd,
            env: {
                ...process.env,
                QQCODE_VSCODE_CONTEXT: contextPath,
                // Ensure Python output is unbuffered when running as subprocess
                PYTHONUNBUFFERED: '1'
            }
        };

        this.outputChannel.appendLine(`[QQCode] Spawning: ${command} ${args.join(' ')}`);
        this.outputChannel.appendLine(`[QQCode] Working directory: ${cwd}`);

        try {
            this.currentProcess = spawn(command, args, spawnOptions);
            this.outputChannel.appendLine(`[QQCode] Process spawned with PID: ${this.currentProcess.pid}`);
        } catch (spawnError) {
            this.outputChannel.appendLine(`[QQCode] Spawn error: ${spawnError}`);
            throw spawnError;
        }

        if (!this.currentProcess.stdout) {
            this.outputChannel.appendLine(`[QQCode] ERROR: No stdout stream available`);
            throw new Error('Failed to spawn QQCode process - no stdout');
        }

        this.outputChannel.appendLine(`[QQCode] stdout stream available, setting up handlers...`);

        // Set up stderr capture IMMEDIATELY
        let stderrBuffer = '';
        if (this.currentProcess.stderr) {
            this.currentProcess.stderr.on('data', (data) => {
                const text = data.toString();
                stderrBuffer += text;
                this.outputChannel.appendLine(`[QQCode stderr] ${text}`);
            });
            this.outputChannel.appendLine(`[QQCode] stderr handler attached`);
        }

        // Track process state
        let processExited = false;
        let exitCode: number | null = null;
        let processError: Error | null = null;

        // Set up process event handlers IMMEDIATELY
        const processExitPromise = new Promise<void>((resolve, reject) => {
            this.currentProcess?.on('exit', (code) => {
                processExited = true;
                exitCode = code;
                this.outputChannel.appendLine(`[QQCode] Process exited with code ${code}`);
                if (code === 0) {
                    resolve();
                } else {
                    const errorMsg = stderrBuffer
                        ? `QQCode exited with code ${code}: ${stderrBuffer}`
                        : `QQCode exited with code ${code}`;
                    reject(new Error(errorMsg));
                }
            });

            this.currentProcess?.on('error', (error) => {
                processExited = true;
                processError = error;
                this.outputChannel.appendLine(`[QQCode] Process error: ${error.message}`);
                reject(error);
            });
        });

        // Parse line-delimited JSON events
        const rl = readline.createInterface({
            input: this.currentProcess.stdout,
            crlfDelay: Infinity
        });

        // Track if we've received any events
        let receivedEvents = false;

        this.outputChannel.appendLine(`[QQCode] Starting to read stdout lines...`);

        try {
            // Process lines as they come
            for await (const line of rl) {
                this.outputChannel.appendLine(`[QQCode] Received line: ${line.substring(0, 100)}...`);
                // Check if process has already exited with an error
                if (processExited && exitCode !== 0) {
                    break;
                }

                if (!line.trim()) {
                    continue;
                }

                try {
                    receivedEvents = true;
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
            rl.close();

            // Clean up context file
            if (contextPath && fs.existsSync(contextPath)) {
                fs.unlinkSync(contextPath);
            }
        }

        // Wait for process to fully exit
        try {
            await processExitPromise;
        } catch (error) {
            // If we received no events and process failed, report the error
            if (!receivedEvents) {
                const errorMessage = error instanceof Error
                    ? error.message
                    : stderrBuffer || `Process failed with exit code ${exitCode}`;
                yield {
                    kind: 'error',
                    message: errorMessage
                };
            }
            throw error;
        }
    }

    /**
     * Convert QQCode JSON event to UI-friendly chunk
     */
    private parseEvent(event: QQCodeEvent): StreamChunk | null {
        switch (event.type) {
            case 'thread.started':
                // Emit session_id if present
                if (event.session_id) {
                    return {
                        kind: 'session_started',
                        sessionId: event.session_id
                    };
                }
                return null;

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
