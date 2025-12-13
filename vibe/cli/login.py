from __future__ import annotations

import asyncio
import webbrowser

from rich import print as rprint
from rich.prompt import Prompt

from vibe.core.config import save_oauth_token
from vibe.core.oauth import (
    exchange_token,
    get_authorize_url,
    get_pkce_challenge,
)


async def _login_anthropic_async() -> None:
    """Interactive OAuth login flow for Anthropic."""
    rprint("\n[bold blue]Anthropic Claude Max Login[/bold blue]\n")

    # Generate PKCE challenge
    verifier, challenge = get_pkce_challenge()
    url = get_authorize_url(challenge, state=verifier)

    rprint("[dim]Opening browser for authentication...[/dim]\n")
    rprint(f"If the browser doesn't open, visit this URL:\n[link={url}]{url}[/link]\n")

    # Try to open browser
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # Get authorization code from user
    code = Prompt.ask("\n[bold]Paste the authorization code from the browser[/bold]")
    code = code.strip()

    if not code:
        rprint("[red]No authorization code provided. Login cancelled.[/red]")
        return

    rprint("\n[dim]Exchanging code for tokens...[/dim]")

    try:
        token = await exchange_token(code, verifier)
    except Exception as e:
        rprint(f"[red]Failed to exchange token: {e}[/red]")
        return

    # Save to config
    try:
        save_oauth_token("anthropic", token)
        rprint("\n[bold green]Successfully logged in to Anthropic![/bold green]")
        rprint("[dim]You can now use Claude models. Run 'vibe' and use /models to select a model.[/dim]\n")
    except Exception as e:
        rprint(f"[red]Failed to save token: {e}[/red]")


def login_anthropic() -> None:
    """Run the Anthropic OAuth login flow."""
    asyncio.run(_login_anthropic_async())
