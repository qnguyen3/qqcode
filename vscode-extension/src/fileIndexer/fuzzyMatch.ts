/**
 * Fuzzy matching algorithm ported from Python implementation
 * Provides scored matching for file path completion
 */

const PREFIX_MULTIPLIER = 2.0;
const WORD_BOUNDARY_MULTIPLIER = 1.8;
const CONSECUTIVE_MULTIPLIER = 1.3;

export interface MatchResult {
    matched: boolean;
    score: number;
    matchedIndices: number[];
}

/**
 * Performs fuzzy matching of a pattern against text
 * @param pattern - The search pattern
 * @param text - The text to search in
 * @param textLower - Pre-lowercased version of text (optional, for performance)
 * @returns Match result with score and matched character indices
 */
export function fuzzyMatch(
    pattern: string,
    text: string,
    textLower?: string
): MatchResult {
    if (!pattern) {
        return { matched: true, score: 0.0, matchedIndices: [] };
    }

    const lowerText = textLower || text.toLowerCase();
    return findBestMatch(pattern, pattern.toLowerCase(), lowerText, text);
}

function findBestMatch(
    patternOriginal: string,
    patternLower: string,
    textLower: string,
    textOriginal: string
): MatchResult {
    if (patternLower.length > textLower.length) {
        return { matched: false, score: 0.0, matchedIndices: [] };
    }

    // Check for prefix match (highest priority)
    if (textLower.startsWith(patternLower)) {
        const indices = Array.from({ length: patternLower.length }, (_, i) => i);
        const score = calculateScore(patternOriginal, patternLower, textLower, indices, textOriginal);
        return {
            matched: true,
            score: score * PREFIX_MULTIPLIER,
            matchedIndices: indices
        };
    }

    // Try different matching strategies
    let bestScore = -1.0;
    let bestIndices: number[] = [];

    const matchers = [
        tryWordBoundaryMatch,
        tryConsecutiveMatch,
        trySubsequenceMatch
    ];

    for (const matcher of matchers) {
        const match = matcher(patternOriginal, patternLower, textLower, textOriginal);
        if (match.matched && match.score > bestScore) {
            bestScore = match.score;
            bestIndices = match.matchedIndices;
        }
    }

    if (bestScore >= 0) {
        return { matched: true, score: bestScore, matchedIndices: bestIndices };
    }

    return { matched: false, score: 0.0, matchedIndices: [] };
}

function tryWordBoundaryMatch(
    patternOriginal: string,
    pattern: string,
    textLower: string,
    textOriginal: string
): MatchResult {
    const indices: number[] = [];
    let patternIdx = 0;

    for (let i = 0; i < textLower.length; i++) {
        if (patternIdx >= pattern.length) {
            break;
        }

        const isBoundary =
            i === 0 ||
            '/-_.'.includes(textLower[i - 1]) ||
            (textOriginal[i] === textOriginal[i].toUpperCase() &&
                textOriginal[i - 1] === textOriginal[i - 1].toLowerCase());

        if (textLower[i] === pattern[patternIdx]) {
            if (isBoundary || (indices.length > 0 && i === indices[indices.length - 1] + 1) || indices.length === 0) {
                indices.push(i);
                patternIdx++;
            }
        }
    }

    if (patternIdx === pattern.length) {
        const score = calculateScore(patternOriginal, pattern, textLower, indices, textOriginal);
        return {
            matched: true,
            score: score * WORD_BOUNDARY_MULTIPLIER,
            matchedIndices: indices
        };
    }

    return { matched: false, score: 0.0, matchedIndices: [] };
}

function tryConsecutiveMatch(
    patternOriginal: string,
    pattern: string,
    textLower: string,
    textOriginal: string
): MatchResult {
    const indices: number[] = [];
    let patternIdx = 0;

    for (let i = 0; i < textLower.length; i++) {
        if (patternIdx >= pattern.length) {
            break;
        }

        if (textLower[i] === pattern[patternIdx]) {
            indices.push(i);
            patternIdx++;
        } else if (indices.length > 0) {
            indices.length = 0;
            patternIdx = 0;
        }
    }

    if (patternIdx === pattern.length) {
        const score = calculateScore(patternOriginal, pattern, textLower, indices, textOriginal);
        return {
            matched: true,
            score: score * CONSECUTIVE_MULTIPLIER,
            matchedIndices: indices
        };
    }

    return { matched: false, score: 0.0, matchedIndices: [] };
}

function trySubsequenceMatch(
    patternOriginal: string,
    pattern: string,
    textLower: string,
    textOriginal: string
): MatchResult {
    const indices: number[] = [];
    let patternIdx = 0;

    for (let i = 0; i < textLower.length; i++) {
        if (patternIdx >= pattern.length) {
            break;
        }
        if (textLower[i] === pattern[patternIdx]) {
            indices.push(i);
            patternIdx++;
        }
    }

    if (patternIdx === pattern.length) {
        const score = calculateScore(patternOriginal, pattern, textLower, indices, textOriginal);
        return { matched: true, score, matchedIndices: indices };
    }

    return { matched: false, score: 0.0, matchedIndices: [] };
}

function calculateScore(
    patternOriginal: string,
    pattern: string,
    textLower: string,
    indices: number[],
    textOriginal: string
): number {
    if (indices.length === 0) {
        return 0.0;
    }

    // Base score
    let baseScore = 100.0;
    if (indices[0] === 0) {
        baseScore += 50.0;
    } else {
        baseScore -= indices[0] * 2;
    }

    // Consecutive character bonus
    let consecutiveBonus = 0.0;
    for (let i = 0; i < indices.length - 1; i++) {
        if (indices[i + 1] === indices[i] + 1) {
            consecutiveBonus += 10.0;
        }
    }

    // Word boundary bonus
    let boundaryBonus = 0.0;
    for (const idx of indices) {
        if (idx === 0 || '/-_.'.includes(textLower[idx - 1])) {
            boundaryBonus += 5.0;
        } else if (
            textOriginal[idx] === textOriginal[idx].toUpperCase() &&
            (idx === 0 || textOriginal[idx - 1] === textOriginal[idx - 1].toLowerCase())
        ) {
            boundaryBonus += 3.0;
        }
    }

    // Case match bonus
    let caseBonus = 0.0;
    for (let i = 0; i < indices.length; i++) {
        const textIdx = indices[i];
        if (
            i < patternOriginal.length &&
            textIdx < textOriginal.length &&
            patternOriginal[i] === textOriginal[textIdx]
        ) {
            caseBonus += 2.0;
        }
    }

    // Gap penalty
    let gapPenalty = 0.0;
    for (let i = 0; i < indices.length - 1; i++) {
        gapPenalty += (indices[i + 1] - indices[i] - 1) * 1.5;
    }

    return Math.max(0.0, baseScore + consecutiveBonus + boundaryBonus + caseBonus - gapPenalty);
}
