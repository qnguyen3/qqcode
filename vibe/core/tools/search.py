from __future__ import annotations

from dataclasses import dataclass
from math import log
import re

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
BM25_K1 = 1.5
BM25_B = 0.75
TOOL_SEARCH_TOOL_NAME = "tool_search"


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def bm25_scores(query: str, documents: dict[str, str]) -> dict[str, float]:
    """Rank ``documents`` (keyed by tool name) against ``query`` with BM25."""
    if not documents:
        return {}
    tokenized = {key: _tokenize(text) for key, text in documents.items()}
    doc_count = len(tokenized)
    avg_len = sum(len(tokens) for tokens in tokenized.values()) / doc_count or 1.0

    query_terms = list(dict.fromkeys(_tokenize(query)))
    if not query_terms:
        return {key: 0.0 for key in documents}

    scores: dict[str, float] = {}
    for key, tokens in tokenized.items():
        length = len(tokens) or 1
        score = 0.0
        for term in query_terms:
            term_freq = tokens.count(term)
            if term_freq == 0:
                continue
            doc_freq = sum(1 for other in tokenized.values() if term in other)
            idf = log(1 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))
            denominator = term_freq + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len)
            score += idf * term_freq * (BM25_K1 + 1) / denominator
        scores[key] = score
    return scores


@dataclass(frozen=True)
class ToolMatch:
    """A ranked tool from the deferred pool."""

    name: str
    description: str
    score: float


def search_tools(
    query: str, pool: dict[str, str], *, limit: int = 8
) -> list[ToolMatch]:
    """Rank deferred tools by name and description for ``query``."""
    documents = {
        name: " ".join(part for part in (name, description) if part)
        for name, description in pool.items()
    }
    scores = bm25_scores(query, documents)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [
        ToolMatch(name=name, description=pool.get(name, ""), score=score)
        for name, score in ranked
        if score > 0
    ][:limit]
