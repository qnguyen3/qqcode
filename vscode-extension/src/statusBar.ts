import * as vscode from 'vscode';

/**
 * QQCode Status Bar Manager
 * Displays agent status and provides quick access to the chat panel
 */
export class QQCodeStatusBar {
    private statusBarItem: vscode.StatusBarItem;
    private animationInterval: NodeJS.Timeout | null = null;
    private spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
    private spinnerIndex = 0;

    constructor() {
        this.statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right,
            100
        );
        this.statusBarItem.command = 'qqcode.openChat';
        this.setReady();
        this.statusBarItem.show();
    }

    /**
     * Set status to ready/idle state
     */
    setReady(): void {
        this.stopAnimation();
        this.statusBarItem.text = '$(sparkle) QQCode';
        this.statusBarItem.tooltip = 'Click to open QQCode chat';
        this.statusBarItem.backgroundColor = undefined;
    }

    /**
     * Set status to thinking/processing state
     */
    setThinking(): void {
        this.statusBarItem.tooltip = 'QQCode is thinking...';
        this.startAnimation('Thinking');
    }

    /**
     * Set status to running a specific tool
     */
    setRunningTool(toolName: string): void {
        const formattedName = this.formatToolName(toolName);
        this.statusBarItem.tooltip = `Running: ${formattedName}`;
        this.startAnimation(formattedName);
    }

    /**
     * Set status to waiting for approval
     */
    setWaitingApproval(toolName: string): void {
        this.stopAnimation();
        const formattedName = this.formatToolName(toolName);
        this.statusBarItem.text = `$(shield) QQCode: Approve ${formattedName}?`;
        this.statusBarItem.tooltip = `Click to approve or reject ${formattedName}`;
        this.statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    }

    /**
     * Set status to plan review mode
     */
    setPlanReview(): void {
        this.stopAnimation();
        this.statusBarItem.text = '$(checklist) QQCode: Review Plan';
        this.statusBarItem.tooltip = 'A plan is ready for your review';
        this.statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    }

    /**
     * Set status to error state
     */
    setError(message?: string): void {
        this.stopAnimation();
        this.statusBarItem.text = '$(error) QQCode';
        this.statusBarItem.tooltip = message ? `Error: ${message}` : 'An error occurred';
        this.statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');

        // Auto-reset after 5 seconds
        setTimeout(() => this.setReady(), 5000);
    }

    /**
     * Set status to streaming response
     */
    setStreaming(): void {
        this.statusBarItem.tooltip = 'QQCode is responding...';
        this.startAnimation('Streaming');
    }

    /**
     * Dispose of the status bar item
     */
    dispose(): void {
        this.stopAnimation();
        this.statusBarItem.dispose();
    }

    // Private methods

    private startAnimation(label: string): void {
        this.stopAnimation();
        this.spinnerIndex = 0;

        this.animationInterval = setInterval(() => {
            const frame = this.spinnerFrames[this.spinnerIndex];
            this.statusBarItem.text = `${frame} QQCode: ${label}`;
            this.spinnerIndex = (this.spinnerIndex + 1) % this.spinnerFrames.length;
        }, 80);
    }

    private stopAnimation(): void {
        if (this.animationInterval) {
            clearInterval(this.animationInterval);
            this.animationInterval = null;
        }
        this.statusBarItem.backgroundColor = undefined;
    }

    private formatToolName(toolName: string): string {
        // Convert snake_case to Title Case
        return toolName
            .split('_')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');
    }
}
