from __future__ import annotations

import time
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


class OAuthToken(BaseModel):
    """OAuth token with access token, refresh token, and expiration info.

    Provider-specific extras (account id, enterprise domain, resource URL,
    issued client id, ...) live in :attr:`metadata` so one token shape serves
    every flow. Unknown top-level fields read from older config files are
    folded into ``metadata`` during validation.
    """

    access_token: str
    refresh_token: str
    expires_in: int  # Seconds until expiration
    expires_at: int  # Unix timestamp when token expires
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_extras(cls, data: Any) -> Any:
        """Fold unknown top-level keys (e.g. legacy ``resource_url``) into metadata."""
        if not isinstance(data, dict):
            return data

        known = set(cls.model_fields)
        extras = {key: value for key, value in data.items() if key not in known}
        if not extras:
            return data

        metadata = dict(data.get("metadata") or {})
        for key, value in extras.items():
            metadata.setdefault(key, value)
        known_values = {key: value for key, value in data.items() if key in known}
        return {**known_values, "metadata": metadata}

    @property
    def resource_url(self) -> str | None:
        """Provider resource URL carried in metadata, if any."""
        value = self.metadata.get("resource_url")
        return value if isinstance(value, str) else None

    def is_expired(self, skew: float = 0.0) -> bool:
        """Check if token is expired, keeping the 10% buffer and extra skew."""
        buffer = max(self.expires_in // 10, skew)
        return time.time() >= (self.expires_at - buffer)

    @classmethod
    def from_token_response(
        cls,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        metadata: dict[str, Any] | None = None,
    ) -> Self:
        """Create an OAuthToken from a token response."""
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            expires_at=int(time.time()) + expires_in,
            metadata=metadata or {},
        )


class AnthropicOAuthToken(OAuthToken):
    """Anthropic OAuth token for Claude API authentication."""


class QwenOAuthToken(OAuthToken):
    """Qwen OAuth token carrying its dynamic resource URL in metadata."""

    @classmethod
    def from_token_response(
        cls,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        metadata: dict[str, Any] | None = None,
        *,
        resource_url: str | None = None,
    ) -> Self:
        """Create a QwenOAuthToken from a token response."""
        merged = dict(metadata or {})
        if resource_url is not None:
            merged["resource_url"] = resource_url
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            expires_at=int(time.time()) + expires_in,
            metadata=merged,
        )
