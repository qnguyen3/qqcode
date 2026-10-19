from __future__ import annotations

import base64
import hashlib
import secrets


def base64_url_encode(data: bytes) -> str:
    """Encode bytes as URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64_url_decode(value: str) -> bytes:
    """Decode a URL-safe base64 string, restoring stripped padding."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def generate_pkce() -> tuple[str, str]:
    """Generate a PKCE ``(verifier, challenge)`` pair (S256)."""
    verifier = base64_url_encode(secrets.token_bytes(32))
    challenge = base64_url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state(nbytes: int = 16) -> str:
    """Generate a random hex state value."""
    return secrets.token_hex(nbytes)


def random_base64url(nbytes: int = 32) -> str:
    """Generate a random URL-safe base64 string."""
    return base64_url_encode(secrets.token_bytes(nbytes))
