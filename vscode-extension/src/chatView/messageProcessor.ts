/**
 * Message processor for handling @ mentions
 * Extracts file/directory mentions and embeds their content
 */

import * as vscode from 'vscode';
import * as path from 'path';

const DEFAULT_MAX_EMBED_BYTES = 256 * 1024; // 256KB

export interface MentionedFile {
    /** Original mention text (e.g., "@src/index.ts") */
    mention: string;
    /** Resolved file path */
    path: string;
    /** File URI */
    uri: vscode.Uri;
    /** Whether this is a directory */
    isDirectory: boolean;
}

export interface ProcessedMessage {
    /** The message text with @ mentions preserved */
    displayText: string;
    /** The message text to send to the backend (with embedded content) */
    promptText: string;
    /** List of mentioned files */
    mentions: MentionedFile[];
}

/**
 * Process a user message to extract @ mentions and embed file contents
 */
export async function processMessage(
    message: string,
    workspaceRoot: vscode.Uri
): Promise<ProcessedMessage> {
    if (!message) {
        return {
            displayText: message,
            promptText: message,
            mentions: []
        };
    }

    const mentions: MentionedFile[] = [];
    const promptParts: string[] = [];
    let pos = 0;

    while (pos < message.length) {
        if (isPathAnchor(message, pos)) {
            const [candidate, newPos] = extractCandidate(message, pos + 1);
            if (candidate) {
                const mention = await resolveToMention(candidate, workspaceRoot);
                if (mention) {
                    mentions.push(mention);
                    promptParts.push(candidate);
                    pos = newPos;
                    continue;
                }
            }
        }

        promptParts.push(message[pos]);
        pos++;
    }

    const promptText = promptParts.join('');
    const uniqueMentions = deduplicateMentions(mentions);
    const finalPrompt = await buildPromptWithEmbeds(promptText, uniqueMentions);

    return {
        displayText: message,
        promptText: finalPrompt,
        mentions: uniqueMentions
    };
}

/**
 * Check if the character at pos is a path anchor (@)
 */
function isPathAnchor(message: string, pos: number): boolean {
    if (message[pos] !== '@') {
        return false;
    }
    if (pos === 0) {
        return true;
    }
    // @ must not be preceded by alphanumeric or underscore
    const prevChar = message[pos - 1];
    return !/[a-zA-Z0-9_]/.test(prevChar);
}

/**
 * Extract the path candidate after @
 * Returns [candidate, newPosition]
 */
function extractCandidate(message: string, start: number): [string | null, number] {
    if (start >= message.length) {
        return [null, start];
    }

    const quote = message[start];
    if (quote === "'" || quote === '"') {
        // Quoted path
        const endQuote = message.indexOf(quote, start + 1);
        if (endQuote === -1) {
            return [null, start];
        }
        return [message.substring(start + 1, endQuote), endQuote + 1];
    }

    // Unquoted path
    let end = start;
    while (end < message.length && isPathChar(message[end])) {
        end++;
    }

    if (end === start) {
        return [null, start];
    }

    return [message.substring(start, end), end];
}

/**
 * Check if a character is valid in an unquoted path
 */
function isPathChar(char: string): boolean {
    return /[a-zA-Z0-9._/\\()\[\]{}-]/.test(char);
}

/**
 * Resolve a candidate path to a mentioned file
 */
async function resolveToMention(
    candidate: string,
    workspaceRoot: vscode.Uri
): Promise<MentionedFile | null> {
    if (!candidate) {
        return null;
    }

    try {
        // Resolve path relative to workspace root
        const candidatePath = path.isAbsolute(candidate)
            ? vscode.Uri.file(candidate)
            : vscode.Uri.joinPath(workspaceRoot, candidate);

        // Check if file/directory exists
        const stat = await vscode.workspace.fs.stat(candidatePath);
        const isDirectory = stat.type === vscode.FileType.Directory;

        return {
            mention: `@${candidate}`,
            path: candidate,
            uri: candidatePath,
            isDirectory
        };
    } catch (error) {
        // File doesn't exist or can't be accessed
        return null;
    }
}

/**
 * Remove duplicate mentions (same path)
 */
function deduplicateMentions(mentions: MentionedFile[]): MentionedFile[] {
    const seen = new Set<string>();
    const unique: MentionedFile[] = [];

    for (const mention of mentions) {
        const key = mention.uri.toString();
        if (!seen.has(key)) {
            seen.add(key);
            unique.push(mention);
        }
    }

    return unique;
}

/**
 * Build the final prompt with embedded file contents
 */
async function buildPromptWithEmbeds(
    promptText: string,
    mentions: MentionedFile[]
): Promise<string> {
    if (mentions.length === 0) {
        return promptText;
    }

    const parts: string[] = [promptText];

    for (const mention of mentions) {
        if (mention.isDirectory) {
            // For directories, just add a reference
            parts.push(`\n\nuri: ${mention.uri.toString()}`);
            parts.push(`name: ${mention.path}`);
        } else {
            // For files, try to embed content
            const embedded = await tryEmbedFile(mention);
            if (embedded) {
                parts.push(`\n\n${embedded}`);
            } else {
                // File too large or binary, add reference
                parts.push(`\n\nuri: ${mention.uri.toString()}`);
                parts.push(`name: ${mention.path}`);
            }
        }
    }

    return parts.join('');
}

/**
 * Try to embed a file's content
 * Returns null if file is too large or binary
 */
async function tryEmbedFile(mention: MentionedFile): Promise<string | null> {
    try {
        const data = await vscode.workspace.fs.readFile(mention.uri);

        // Check size limit
        if (data.length > DEFAULT_MAX_EMBED_BYTES) {
            return null;
        }

        // Check if it's probably text
        if (!isProbablyText(mention.path, data)) {
            return null;
        }

        // Try to decode as UTF-8
        const text = new TextDecoder('utf-8', { fatal: true }).decode(data);

        // Format as code block
        return `${mention.uri.toString()}\n\`\`\`\n${text}\n\`\`\``;
    } catch (error) {
        // Failed to read or decode
        return null;
    }
}

/**
 * Check if file is probably text (not binary)
 */
function isProbablyText(filePath: string, data: Uint8Array): boolean {
    // Check MIME type
    const ext = path.extname(filePath).toLowerCase();
    const binaryExtensions = [
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico', '.svg',
        '.mp3', '.mp4', '.avi', '.mov', '.wav',
        '.zip', '.tar', '.gz', '.rar', '.7z',
        '.exe', '.dll', '.so', '.dylib',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx'
    ];

    if (binaryExtensions.includes(ext)) {
        return false;
    }

    // Check for null bytes
    if (data.includes(0)) {
        return false;
    }

    // Check proportion of non-printable characters
    const DEL_CODE = 127;
    const NON_PRINTABLE_MAX_PROPORTION = 0.1;
    const NON_PRINTABLE_MAX_CODE = 31;
    const NON_PRINTABLE_EXCEPTIONS = [9, 10, 11, 12]; // Tab, LF, VT, FF

    let nonText = 0;
    for (const byte of data) {
        if (
            (byte <= NON_PRINTABLE_MAX_CODE && !NON_PRINTABLE_EXCEPTIONS.includes(byte)) ||
            byte === DEL_CODE
        ) {
            nonText++;
        }
    }

    return (nonText / data.length) < NON_PRINTABLE_MAX_PROPORTION;
}
