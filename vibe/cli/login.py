from __future__ import annotations

import asyncio
from collections.abc import Sequence
import webbrowser

from rich import print as rprint
from rich.prompt import Prompt

from vibe.core.ambient import ambient_setup
from vibe.core.auth import (
    AuthKind,
    ProviderAuth,
    all_provider_auths,
    find_provider,
    logout,
    provider_auths,
    save_api_key,
)
from vibe.core.config import (
    ProviderConfig,
    VibeConfig,
    save_oauth_token,
    skip_api_key_check,
)
from vibe.core.oauth import (
    DeviceCodeInfo,
    OAuthFlow,
    SelectOption,
    UnknownOAuthFlowError,
    get_flow,
)


class RichAuthInteraction:
    """Rich-based ``AuthInteraction`` driving any registered OAuth flow."""

    def __init__(self) -> None:
        self.cancel_event = asyncio.Event()

    async def prompt_secret(self, message: str) -> str:
        return await asyncio.to_thread(
            Prompt.ask, f"[bold]{message}[/bold]", password=True
        )

    async def prompt_text(self, message: str, placeholder: str | None = None) -> str:
        prompt = f"[bold]{message}[/bold]"
        if placeholder:
            prompt = f"{prompt} [dim]({placeholder})[/dim]"
        return await asyncio.to_thread(Prompt.ask, prompt)

    async def select(self, message: str, options: Sequence[SelectOption]) -> str:
        def ask() -> str:
            for index, option in enumerate(options, start=1):
                description = (
                    f" [dim]{option.description}[/dim]" if option.description else ""
                )
                rprint(f"  [bold]{index}.[/] {option.label}{description}")
            choices = [str(index) for index in range(1, len(options) + 1)]
            answer = Prompt.ask(f"\n{message}", choices=choices, default="1")
            return options[int(answer) - 1].id

        return await asyncio.to_thread(ask)

    async def show_auth_url(self, url: str, instructions: str | None = None) -> None:
        if instructions:
            rprint(f"[dim]{instructions}[/dim]")
        rprint(
            f"If the browser doesn't open, visit this URL:\n[link={url}]{url}[/link]\n"
        )
        try:
            webbrowser.open(url)
        except Exception:
            pass

    async def show_device_code(self, info: DeviceCodeInfo) -> None:
        rprint(
            "Please visit this URL to authorize:\n"
            f"[link={info.verification_uri}]{info.verification_uri}[/link]\n"
            f"and enter the code: [bold]{info.user_code}[/bold]\n"
        )
        try:
            webbrowser.open(info.verification_uri)
        except Exception:
            pass

    async def progress(self, message: str) -> None:
        rprint(f"[dim]{message}[/dim]")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


async def _login_registered_flow_async(flow: OAuthFlow, provider_name: str) -> None:
    rprint(f"\n[bold blue]{provider_name} Login[/bold blue]\n")
    interaction = RichAuthInteraction()
    try:
        token = await flow.login(interaction)
    except KeyboardInterrupt:
        rprint("\n[yellow]Login cancelled.[/yellow]")
        return
    except Exception as exc:
        rprint(f"[red]Login failed: {exc}[/red]")
        return

    try:
        save_oauth_token(provider_name, token)
    except Exception as exc:
        rprint(f"[red]Failed to save token: {exc}[/red]")
        return

    rprint(f"\n[bold green]Successfully logged in to {provider_name}![/bold green]")
    rprint(
        "[dim]You can now use this provider. Run 'qqcode' and use /models to "
        "select a model.[/dim]\n"
    )


def _login_registered_flow(provider_name: str) -> None:
    try:
        flow = get_flow(provider_name)
    except UnknownOAuthFlowError:
        rprint(
            f"[red]No interactive login flow is available for '{provider_name}'.[/red]"
        )
        return
    asyncio.run(_login_registered_flow_async(flow, provider_name))


def login_anthropic() -> None:
    """Run the Anthropic OAuth login flow."""
    _login_registered_flow("anthropic")


def login_qwen() -> None:
    """Run the Qwen OAuth login flow."""
    _login_registered_flow("qwen")


def _select(prompt: str, options: list[str]) -> int:
    for index, option in enumerate(options, start=1):
        rprint(f"  [bold]{index}.[/] {option}")
    choices = [str(index) for index in range(1, len(options) + 1)]
    answer = Prompt.ask(f"\n{prompt}", choices=choices, default="1")
    return int(answer) - 1


def _status_suffix(auth: ProviderAuth) -> str:
    if auth.source:
        return f" [green]✓ {auth.source}[/]"
    return " [dim]not configured[/]"


def _login_oauth(provider_name: str) -> None:
    match provider_name:
        case "anthropic":
            login_anthropic()
        case "qwen":
            login_qwen()
        case _:
            _login_registered_flow(provider_name)


def _login_api_key(provider: ProviderConfig) -> None:
    env_var = provider.api_key_env_var
    key = Prompt.ask(
        f"[bold]Enter {env_var} for {provider.name}[/bold]", password=True
    ).strip()
    if not key:
        rprint("[red]No API key provided. Login cancelled.[/red]")
        return

    try:
        path = save_api_key(provider, key)
    except (OSError, ValueError) as exc:
        rprint(f"[red]Failed to save API key: {exc}[/red]")
        return

    rprint(f"\n[bold green]Saved {env_var} to {path}[/bold green]")
    rprint("[dim]New QQcode sessions will use this key.[/dim]\n")


def _login_ambient(provider: ProviderConfig) -> None:
    rprint(f"\n[bold blue]{provider.name} uses ambient credentials.[/bold blue]\n")
    rprint(f"{ambient_setup(provider.ambient)}")
    rprint(
        "[dim]QQcode does not store a key for this provider; configure the "
        "credentials above and restart.[/dim]\n"
    )


def _run_auth(config: VibeConfig, auth: ProviderAuth) -> None:
    provider = find_provider(config, auth.provider)
    if provider is None:
        rprint(f"[red]Unknown provider '{auth.provider}'.[/red]")
        return
    match auth.kind:
        case "oauth":
            _login_oauth(provider.name)
        case "api_key":
            _login_api_key(provider)
        case "ambient":
            _login_ambient(provider)


def login_provider(config: VibeConfig, name: str) -> None:
    """Log in to one provider, asking how when it offers several methods."""
    auths = provider_auths(config, name)
    match auths:
        case []:
            rprint(
                f"[red]Unknown provider '{name}'. Run 'qqcode login' to list "
                "the configured providers.[/red]"
            )
        case [auth]:
            _run_auth(config, auth)
        case _:
            index = _select(
                f"Select authentication method for {name}:",
                [f"{auth.label}{_status_suffix(auth)}" for auth in auths],
            )
            _run_auth(config, auths[index])


def _select_and_login(config: VibeConfig, kind: AuthKind) -> None:
    auths = all_provider_auths(config, kind)
    if not auths:
        rprint(f"[red]No {kind} providers are configured.[/red]")
        return
    index = _select(
        "Select provider:",
        [
            f"{auth.provider} [dim]({auth.label})[/]{_status_suffix(auth)}"
            for auth in auths
        ],
    )
    _run_auth(config, auths[index])


def _load_login_config() -> VibeConfig:
    """Load the configuration without requiring a usable API key."""
    with skip_api_key_check():
        return VibeConfig.load()


def run_login_command(argv: list[str]) -> int:
    """Run ``qqcode login [provider]``."""
    config = _load_login_config()

    match argv:
        case [name]:
            login_provider(config, name)
            return 0
        case []:
            auths = all_provider_auths(config)
            if not auths:
                rprint("[red]No providers are configured.[/red]")
                return 1
            kinds: set[AuthKind] = {auth.kind for auth in auths}
            kind: AuthKind
            if len(kinds) > 1:
                options: list[tuple[AuthKind, str]] = []
                if "oauth" in kinds:
                    options.append(("oauth", "Sign in with a subscription or account"))
                if "api_key" in kinds:
                    options.append(("api_key", "Sign in with an API key"))
                if "ambient" in kinds:
                    options.append(("ambient", "Configure ambient credentials"))
                index = _select(
                    "Select authentication method:", [label for _, label in options]
                )
                kind = options[index][0]
            else:
                kind = next(iter(kinds))
            _select_and_login(config, kind)
            return 0
        case _:
            rprint("[red]Usage: qqcode login [provider][/red]")
            return 1


def _logout(config: VibeConfig, name: str) -> None:
    notes = logout(config, name)
    if not notes:
        rprint(f"[dim]No stored credentials found for '{name}'.[/dim]")
        return
    for note in notes:
        rprint(f"[green]✓[/] {note}")
    rprint("[dim]Environment variables set outside .env are not affected.[/dim]")


def run_logout_command(argv: list[str]) -> int:
    """Run ``qqcode logout [provider]``."""
    config = _load_login_config()

    match argv:
        case [name]:
            _logout(config, name)
            return 0
        case []:
            auths = [
                auth
                for auth in all_provider_auths(config)
                if auth.configured and auth.kind != "ambient"
            ]
            if not auths:
                rprint("[dim]No stored credentials to remove.[/dim]")
                return 0
            index = _select(
                "Select credential to remove:",
                [f"{auth.provider} ({auth.source})" for auth in auths],
            )
            _logout(config, auths[index].provider)
            return 0
        case _:
            rprint("[red]Usage: qqcode logout [provider][/red]")
            return 1
