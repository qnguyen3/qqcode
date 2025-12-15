/**
 * File indexer for workspace files
 * Maintains an in-memory index of all files for fast autocomplete
 */

import * as vscode from 'vscode';
import * as path from 'path';

export interface IndexEntry {
    /** Relative path from workspace root */
    rel: string;
    /** Lowercased relative path for case-insensitive matching */
    relLower: string;
    /** File/directory name */
    name: string;
    /** Full URI */
    uri: vscode.Uri;
    /** Whether this is a directory */
    isDir: boolean;
}

export class FileIndexer {
    private entries: Map<string, IndexEntry> = new Map();
    private orderedEntries: IndexEntry[] | null = null;
    private workspaceRoot: vscode.Uri | null = null;
    private watcher: vscode.FileSystemWatcher | null = null;
    private indexing: Promise<void> | null = null;

    constructor() {}

    /**
     * Get the current file index for the workspace
     * Triggers indexing if not already done
     */
    async getIndex(workspaceRoot: vscode.Uri): Promise<IndexEntry[]> {
        const rootChanged = this.workspaceRoot?.toString() !== workspaceRoot.toString();

        if (rootChanged) {
            this.stopWatching();
            this.clear();
            this.workspaceRoot = workspaceRoot;
        }

        if (this.entries.size === 0 && !this.indexing) {
            this.indexing = this.rebuildIndex(workspaceRoot);
            await this.indexing;
            this.indexing = null;
        } else if (this.indexing) {
            await this.indexing;
        }

        this.startWatching(workspaceRoot);

        return this.snapshot();
    }

    /**
     * Clear the index
     */
    clear(): void {
        this.entries.clear();
        this.orderedEntries = null;
        this.workspaceRoot = null;
    }

    /**
     * Stop watching for file changes
     */
    dispose(): void {
        this.stopWatching();
        this.clear();
    }

    /**
     * Get a snapshot of the current index
     */
    private snapshot(): IndexEntry[] {
        if (!this.orderedEntries) {
            this.orderedEntries = Array.from(this.entries.values()).sort((a, b) =>
                a.rel.localeCompare(b.rel)
            );
        }
        return [...this.orderedEntries];
    }

    /**
     * Rebuild the entire index
     */
    private async rebuildIndex(root: vscode.Uri): Promise<void> {
        this.entries.clear();
        this.orderedEntries = null;

        try {
            await this.indexDirectory(root, '');
        } catch (error) {
            console.error('Failed to index workspace:', error);
        }
    }

    /**
     * Recursively index a directory
     */
    private async indexDirectory(uri: vscode.Uri, relPrefix: string): Promise<void> {
        try {
            const entries = await vscode.workspace.fs.readDirectory(uri);

            for (const [name, type] of entries) {
                // Skip hidden files and common ignore patterns
                if (this.shouldIgnore(name, relPrefix)) {
                    continue;
                }

                const isDir = type === vscode.FileType.Directory;
                const rel = relPrefix ? `${relPrefix}/${name}` : name;
                const childUri = vscode.Uri.joinPath(uri, name);

                const entry: IndexEntry = {
                    rel,
                    relLower: rel.toLowerCase(),
                    name,
                    uri: childUri,
                    isDir
                };

                this.entries.set(rel, entry);

                // Recursively index subdirectories
                if (isDir) {
                    await this.indexDirectory(childUri, rel);
                }
            }
        } catch (error) {
            // Ignore permission errors and continue
            console.debug(`Failed to index directory ${uri.fsPath}:`, error);
        }
    }

    /**
     * Check if a file/directory should be ignored
     */
    private shouldIgnore(name: string, relPath: string): boolean {
        // Hidden files (unless explicitly searching for them)
        if (name.startsWith('.') && name !== '.') {
            // Allow some common config files
            const allowedHidden = ['.gitignore', '.env', '.editorconfig'];
            if (!allowedHidden.includes(name)) {
                return true;
            }
        }

        // Common ignore patterns
        const ignorePatterns = [
            'node_modules',
            '__pycache__',
            '.git',
            '.vscode',
            '.idea',
            'dist',
            'build',
            'out',
            '.next',
            '.nuxt',
            'coverage',
            '.pytest_cache',
            '.mypy_cache',
            'venv',
            '.venv',
            'env',
            '.DS_Store',
            'Thumbs.db'
        ];

        return ignorePatterns.includes(name);
    }

    /**
     * Start watching for file changes
     */
    private startWatching(root: vscode.Uri): void {
        if (this.watcher) {
            return;
        }

        // Create a file system watcher for all files
        const pattern = new vscode.RelativePattern(root, '**/*');
        this.watcher = vscode.workspace.createFileSystemWatcher(pattern);

        // Handle file creation
        this.watcher.onDidCreate(async (uri) => {
            await this.handleFileCreated(uri);
        });

        // Handle file deletion
        this.watcher.onDidDelete((uri) => {
            this.handleFileDeleted(uri);
        });

        // Handle file changes (we don't need to update index for content changes)
        // this.watcher.onDidChange(() => {});
    }

    /**
     * Stop watching for file changes
     */
    private stopWatching(): void {
        if (this.watcher) {
            this.watcher.dispose();
            this.watcher = null;
        }
    }

    /**
     * Handle file creation
     */
    private async handleFileCreated(uri: vscode.Uri): Promise<void> {
        if (!this.workspaceRoot) {
            return;
        }

        try {
            const rel = path.relative(this.workspaceRoot.fsPath, uri.fsPath);
            if (!rel || rel.startsWith('..')) {
                return;
            }

            const name = path.basename(uri.fsPath);
            if (this.shouldIgnore(name, path.dirname(rel))) {
                return;
            }

            const stat = await vscode.workspace.fs.stat(uri);
            const isDir = stat.type === vscode.FileType.Directory;

            const entry: IndexEntry = {
                rel: rel.replace(/\\/g, '/'), // Normalize to forward slashes
                relLower: rel.toLowerCase().replace(/\\/g, '/'),
                name,
                uri,
                isDir
            };

            this.entries.set(entry.rel, entry);
            this.orderedEntries = null; // Invalidate cache

            // If it's a directory, index its contents
            if (isDir) {
                await this.indexDirectory(uri, entry.rel);
            }
        } catch (error) {
            console.debug('Failed to handle file creation:', error);
        }
    }

    /**
     * Handle file deletion
     */
    private handleFileDeleted(uri: vscode.Uri): void {
        if (!this.workspaceRoot) {
            return;
        }

        try {
            const rel = path.relative(this.workspaceRoot.fsPath, uri.fsPath).replace(/\\/g, '/');
            if (!rel || rel.startsWith('..')) {
                return;
            }

            const entry = this.entries.get(rel);
            if (!entry) {
                return;
            }

            this.entries.delete(rel);

            // If it's a directory, remove all children
            if (entry.isDir) {
                const prefix = `${rel}/`;
                for (const key of this.entries.keys()) {
                    if (key.startsWith(prefix)) {
                        this.entries.delete(key);
                    }
                }
            }

            this.orderedEntries = null; // Invalidate cache
        } catch (error) {
            console.debug('Failed to handle file deletion:', error);
        }
    }
}
