from __future__ import annotations

from vibe.core.tools.search import bm25_scores, search_tools


def test_bm25_ranks_matching_documents_higher() -> None:
    scores = bm25_scores(
        "create issue",
        {
            "create_issue": "create a github issue with a title",
            "list_files": "list files in a directory",
            "echo": "echo text back",
        },
    )

    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    assert ranked[0] == "create_issue"
    assert scores["echo"] == 0


def test_search_tools_matches_underscored_names() -> None:
    pool = {
        "mcp__serena__find_symbol": "Find a symbol in the codebase",
        "mcp__serena__list_dir": "List directory contents",
    }

    matches = search_tools("find symbol", pool)

    assert matches[0].name == "mcp__serena__find_symbol"
    assert matches[0].score > 0


def test_search_tools_respects_limit() -> None:
    pool = {f"tool_{index}": "shared description" for index in range(20)}

    matches = search_tools("shared", pool, limit=5)

    assert len(matches) == 5


def test_search_tools_empty_query_returns_nothing() -> None:
    matches = search_tools("", {"todo": "manage the todo list"})

    assert matches == []
