/**
 * Path completer for @ mentions
 * Provides file/directory suggestions based on user input
 */

import * as vscode from 'vscode';
import { FileIndexer, IndexEntry } from './FileIndexer';
import { fuzzyMatch } from './fuzzyMatch';

const DEFAULT_MAX_ENTRIES_TO_PROCESS = 32000;
const DEFAULT_TARGET_MATCHES = 100;
const MAX_SUGGESTIONS = 50;

export interface CompletionSuggestion {
    /** The completion text (e.g., "@src/index.ts") */
    label: string;
    /** The relative path */
    path: string;
    /** Whether this is a directory */
    isDirectory: boolean;
}

interface SearchContext {
    suffix: string;
    searchPattern: string;
    pathPrefix: string;
    immediateOnly: boolean;
}

export class PathCompleter {
    private indexer: FileIndexer;
    private maxEntriesToProcess: number;
    private targetMatches: number;

    constructor(
        indexer: FileIndexer,
        maxEntriesToProcess: number = DEFAULT_MAX_ENTRIES_TO_PROCESS,
        targetMatches: number = DEFAULT_TARGET_MATCHES
    ) {
        this.indexer = indexer;
        this.maxEntriesToProcess = maxEntriesToProcess;
        this.targetMatches = targetMatches;
    }

    /**
     * Get completion suggestions for the given text and cursor position
     */
    async getCompletions(
        text: string,
        cursorPos: number,
        workspaceRoot: vscode.Uri
    ): Promise<CompletionSuggestion[]> {
        const beforeCursor = text.substring(0, cursorPos);
        const partialPath = this.extractPartial(beforeCursor);

        if (partialPath === null) {
            return [];
        }

        const context = this.buildSearchContext(partialPath);

        try {
            const fileIndex = await this.indexer.getIndex(workspaceRoot);
            const scoredMatches = this.scoreMatches(fileIndex, context);
            return scoredMatches.slice(0, MAX_SUGGESTIONS).map(([label, path, isDir]) => ({
                label,
                path,
                isDirectory: isDir
            }));
        } catch (error) {
            console.error('Failed to get completions:', error);
            return [];
        }
    }

    /**
     * Get the replacement range for a completion
     * Returns [start, end] indices in the text
     */
    getReplacementRange(text: string, cursorPos: number): [number, number] | null {
        const beforeCursor = text.substring(0, cursorPos);
        const atIndex = beforeCursor.lastIndexOf('@');

        if (atIndex === -1) {
            return null;
        }

        return [atIndex, cursorPos];
    }

    /**
     * Extract the partial path after @ symbol
     */
    private extractPartial(beforeCursor: string): string | null {
        if (!beforeCursor.includes('@')) {
            return null;
        }

        const atIndex = beforeCursor.lastIndexOf('@');
        const fragment = beforeCursor.substring(atIndex + 1);

        // Fragment must not contain spaces (unless quoted, but we'll handle that later)
        if (fragment.includes(' ')) {
            return null;
        }

        return fragment;
    }

    /**
     * Build search context based on the partial path
     */
    private buildSearchContext(partialPath: string): SearchContext {
        const suffix = partialPath.split('/').pop() || '';

        if (!partialPath) {
            // "@" => show top-level dir and files
            return {
                searchPattern: '',
                pathPrefix: '',
                suffix,
                immediateOnly: true
            };
        }

        if (partialPath.endsWith('/')) {
            // "@something/" => list immediate children
            return {
                searchPattern: '',
                pathPrefix: partialPath,
                suffix,
                immediateOnly: true
            };
        }

        // => run fuzzy search across the index
        return {
            searchPattern: partialPath,
            pathPrefix: '',
            suffix,
            immediateOnly: false
        };
    }

    /**
     * Check if an entry matches the prefix
     */
    private matchesPrefix(entry: IndexEntry, context: SearchContext): boolean {
        const pathStr = entry.rel;

        if (context.pathPrefix) {
            const prefixWithoutSlash = context.pathPrefix.replace(/\/$/, '');
            const prefixWithSlash = `${prefixWithoutSlash}/`;

            // Don't suggest the dir itself (e.g. "@src/" => don't suggest "@src/")
            if (pathStr === prefixWithoutSlash && entry.isDir) {
                return false;
            }

            let afterPrefix: string;
            if (pathStr.startsWith(prefixWithSlash)) {
                afterPrefix = pathStr.substring(prefixWithSlash.length);
            } else {
                const idx = pathStr.indexOf(prefixWithSlash);
                if (idx === -1 || (idx > 0 && pathStr[idx - 1] !== '/')) {
                    return false;
                }
                afterPrefix = pathStr.substring(idx + prefixWithSlash.length);
            }

            // Only suggest files/dirs that are immediate children of the prefix
            return afterPrefix.length > 0 && !afterPrefix.includes('/');
        }

        if (context.immediateOnly && pathStr.includes('/')) {
            // When user just typed "@", only show top-level entries
            return false;
        }

        return true;
    }

    /**
     * Check if an entry should be visible
     */
    private isVisible(entry: IndexEntry, context: SearchContext): boolean {
        // Hide hidden files unless user is explicitly searching for them
        return !(entry.name.startsWith('.') && !context.suffix.startsWith('.'));
    }

    /**
     * Format the label for display
     */
    private formatLabel(entry: IndexEntry): string {
        const suffix = entry.isDir ? '/' : '';
        return `@${entry.rel}${suffix}`;
    }

    /**
     * Score and sort matches
     */
    private scoreMatches(
        entries: IndexEntry[],
        context: SearchContext
    ): Array<[string, string, boolean]> {
        const scoredMatches: Array<[string, number, string, boolean]> = [];

        for (let i = 0; i < entries.length && i < this.maxEntriesToProcess; i++) {
            const entry = entries[i];

            if (!this.matchesPrefix(entry, context)) {
                continue;
            }

            if (!this.isVisible(entry, context)) {
                continue;
            }

            const label = this.formatLabel(entry);

            if (!context.searchPattern) {
                scoredMatches.push([label, 0.0, entry.rel, entry.isDir]);
                if (scoredMatches.length >= this.targetMatches) {
                    break;
                }
                continue;
            }

            const matchResult = fuzzyMatch(
                context.searchPattern,
                entry.rel,
                entry.relLower
            );

            if (matchResult.matched) {
                scoredMatches.push([label, matchResult.score, entry.rel, entry.isDir]);
                if (scoredMatches.length >= this.targetMatches && matchResult.score > MAX_SUGGESTIONS) {
                    break;
                }
            }
        }

        // Sort by score (descending) then by label (ascending)
        scoredMatches.sort((a, b) => {
            if (b[1] !== a[1]) {
                return b[1] - a[1]; // Higher score first
            }
            return a[0].localeCompare(b[0]); // Alphabetical
        });

        return scoredMatches.map(([label, , path, isDir]) => [label, path, isDir]);
    }
}
