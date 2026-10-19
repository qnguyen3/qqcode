from __future__ import annotations

from vibe.core.oauth.base import OAuthFlow


class UnknownOAuthFlowError(ValueError):
    """Raised when no OAuth flow is registered for a provider."""

    def __init__(self, name: str, available: list[str]) -> None:
        joined = ", ".join(available) if available else "<none>"
        super().__init__(f"Unknown OAuth flow '{name}'. Registered flows: {joined}.")
        self.name = name
        self.available = available


_FLOWS: dict[str, OAuthFlow] = {}


def register_flow(flow: OAuthFlow) -> OAuthFlow:
    """Register an OAuth flow, keyed by its provider name."""
    _FLOWS[flow.name] = flow
    return flow


def get_flow(name: str) -> OAuthFlow:
    """Return the OAuth flow registered for ``name``.

    Raises:
        UnknownOAuthFlowError: If no flow is registered under that name.
    """
    match _FLOWS.get(name):
        case None:
            raise UnknownOAuthFlowError(name, sorted(_FLOWS))
        case flow:
            return flow
