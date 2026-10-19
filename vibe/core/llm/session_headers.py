from __future__ import annotations

from vibe.core.config import ProviderConfig

# OpenCode routes a request by its conversation. OpenCode Go rejects requests
# that lack this header with MissingSessionID, so both OpenCode providers send it.
OPENCODE_SESSION_PROVIDERS = frozenset({"opencode", "opencode-go"})


def session_headers(provider: ProviderConfig, session_id: str) -> dict[str, str]:
    """Return the headers that tie a request to its conversation."""
    if provider.name in OPENCODE_SESSION_PROVIDERS:
        return {"x-opencode-session": session_id}
    return {}
