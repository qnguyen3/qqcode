from __future__ import annotations

import time

from pydantic import BaseModel


class OAuthToken(BaseModel):
    """OAuth token with access token, refresh token, and expiration info."""

    access_token: str
    refresh_token: str
    expires_in: int  # Seconds until expiration
    expires_at: int  # Unix timestamp when token expires

    def is_expired(self) -> bool:
        """Check if token is expired or within 10% of expiration time."""
        buffer = self.expires_in // 10
        return time.time() >= (self.expires_at - buffer)

    @classmethod
    def from_token_response(
        cls, access_token: str, refresh_token: str, expires_in: int
    ) -> OAuthToken:
        """Create an OAuthToken from a token response."""
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            expires_at=int(time.time()) + expires_in,
        )
