from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from vibe.core.oauth.token import OAuthToken

AnyOAuthToken = OAuthToken


@dataclass(frozen=True)
class SelectOption:
    """One choice offered by :meth:`AuthInteraction.select`."""

    id: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class DeviceCodeInfo:
    """Device authorization details shown to the user."""

    user_code: str
    verification_uri: str
    interval_seconds: float | None = None
    expires_in_seconds: float | None = None


class AuthInteraction(Protocol):
    """UI surface a login flow drives.

    Implementations are the CLI (rich prompts) and test fakes. ``cancel_event``
    is set when the user aborts; flows poll :attr:`cancelled` or await the
    event to stop network polling and callback servers.
    """

    cancel_event: asyncio.Event

    @property
    def cancelled(self) -> bool:
        """Whether the user aborted the login."""
        return self.cancel_event.is_set()

    async def prompt_secret(self, message: str) -> str: ...

    async def prompt_text(
        self, message: str, placeholder: str | None = None
    ) -> str: ...

    async def select(self, message: str, options: Sequence[SelectOption]) -> str: ...

    async def show_auth_url(
        self, url: str, instructions: str | None = None
    ) -> None: ...

    async def show_device_code(self, info: DeviceCodeInfo) -> None: ...

    async def progress(self, message: str) -> None: ...


class OAuthFlow(Protocol):
    """Provider login/refresh flow registered under a provider name."""

    name: ClassVar[str]
    is_subscription: ClassVar[bool]
    login_label: ClassVar[str]

    async def login(self, interaction: AuthInteraction) -> AnyOAuthToken: ...

    async def refresh(self, token: AnyOAuthToken) -> AnyOAuthToken: ...
