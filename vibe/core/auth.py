from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, set_key, unset_key

from vibe.core.config import (
    GLOBAL_ENV_FILE,
    ProviderConfig,
    VibeConfig,
    clear_oauth_token,
)
from vibe.core.oauth import get_flow
from vibe.core.oauth.registry import UnknownOAuthFlowError

AuthKind = Literal["oauth", "api_key", "ambient"]


@dataclass(frozen=True)
class ProviderAuth:
    """One way to authenticate a provider."""

    provider: str
    kind: AuthKind
    label: str
    subscription: bool = False
    env_var: str | None = None
    source: str | None = None
    setup: str | None = None

    @property
    def configured(self) -> bool:
        return self.source is not None


def find_provider(config: VibeConfig, name: str) -> ProviderConfig | None:
    """Return the provider with ``name`` from the configuration, if any."""
    for provider in config.providers:
        if provider.name == name:
            return provider
    return None


def _api_key_source(env_var: str) -> str | None:
    if os.environ.get(env_var):
        return f"env: {env_var}"
    if GLOBAL_ENV_FILE.is_file() and dotenv_values(GLOBAL_ENV_FILE).get(env_var):
        return f".env: {env_var}"
    return None


def provider_auths(config: VibeConfig, name: str) -> list[ProviderAuth]:
    """List the authentication methods available for one provider.

    A provider can offer a subscription/account OAuth flow, an API key, or
    both. Each method reports where its credential currently comes from.
    """
    provider = find_provider(config, name)
    if provider is None:
        return []

    auths: list[ProviderAuth] = []
    try:
        flow = get_flow(provider.name)
    except UnknownOAuthFlowError:
        pass
    else:
        auths.append(
            ProviderAuth(
                provider=provider.name,
                kind="oauth",
                label="subscription" if flow.is_subscription else "account",
                subscription=flow.is_subscription,
                source=("OAuth token" if provider.oauth_token is not None else None),
            )
        )

    if provider.api_key_env_var:
        auths.append(
            ProviderAuth(
                provider=provider.name,
                kind="api_key",
                label="API key",
                env_var=provider.api_key_env_var,
                source=_api_key_source(provider.api_key_env_var),
            )
        )

    if provider.ambient:
        from vibe.core.ambient import ambient_setup, resolve_ambient

        credential = resolve_ambient(provider.ambient)
        auths.append(
            ProviderAuth(
                provider=provider.name,
                kind="ambient",
                label="ambient credentials",
                source=credential.source if credential else None,
                setup=ambient_setup(provider.ambient),
            )
        )
    return auths


def all_provider_auths(
    config: VibeConfig, kind: AuthKind | None = None
) -> list[ProviderAuth]:
    """List authentication methods for every configured provider."""
    auths = [
        auth
        for provider in config.providers
        for auth in provider_auths(config, provider.name)
    ]
    if kind is None:
        return auths
    return [auth for auth in auths if auth.kind == kind]


def provider_is_configured(config: VibeConfig, name: str) -> bool:
    """Whether ``name`` has usable credentials.

    Providers that declare no authentication (for example a local model
    server) are always usable without credentials. Unknown providers are
    never configured.
    """
    if find_provider(config, name) is None:
        return False
    auths = provider_auths(config, name)
    if not auths:
        return True
    return any(auth.configured for auth in auths)


def configured_provider_names(config: VibeConfig) -> set[str]:
    """Names of the providers the user can currently send requests to."""
    return {
        provider.name
        for provider in config.providers
        if provider_is_configured(config, provider.name)
    }


def save_api_key(provider: ProviderConfig, key: str) -> Path:
    """Store an API key in the global ``.env`` file and the current process.

    Raises:
        ValueError: If the provider does not use an API key.
        OSError: If the ``.env`` file cannot be written.
    """
    env_var = provider.api_key_env_var
    if not env_var:
        raise ValueError(f"Provider '{provider.name}' does not use an API key")

    GLOBAL_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(GLOBAL_ENV_FILE), env_var, key)
    with suppress(OSError):
        GLOBAL_ENV_FILE.chmod(0o600)
    os.environ[env_var] = key
    return GLOBAL_ENV_FILE


def remove_api_key(provider: ProviderConfig) -> bool:
    """Remove a stored API key from ``.env`` and the current process.

    Returns:
        ``True`` when a key was removed from the ``.env`` file.
    """
    env_var = provider.api_key_env_var
    if not env_var:
        return False

    os.environ.pop(env_var, None)
    if not GLOBAL_ENV_FILE.is_file():
        return False
    removed, _ = unset_key(str(GLOBAL_ENV_FILE), env_var)
    return bool(removed)


def logout(config: VibeConfig, name: str) -> list[str]:
    """Remove every stored credential for a provider.

    Returns:
        Human-readable notes about what was removed; empty when the provider
        is unknown or nothing was stored.
    """
    provider = find_provider(config, name)
    if provider is None:
        return []

    notes: list[str] = []
    if provider.api_key_env_var and remove_api_key(provider):
        notes.append(f"removed {provider.api_key_env_var} from {GLOBAL_ENV_FILE}")
    if clear_oauth_token(provider.name):
        notes.append(f"removed the stored OAuth token for '{provider.name}'")
    return notes
