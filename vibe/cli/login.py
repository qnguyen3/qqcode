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
    qwen_get_pkce_challenge,
    qwen_poll_for_token,
    qwen_request_device_code,
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
        rprint("[dim]You can now use Claude models. Run 'qqcode' and use /models to select a model.[/dim]\n")
    except Exception as e:
        rprint(f"[red]Failed to save token: {e}[/red]")


def login_anthropic() -> None:
    """Run the Anthropic OAuth login flow."""
    asyncio.run(_login_anthropic_async())


async def _login_qwen_async() -> None:
    """Interactive OAuth login flow for Qwen (device authorization)."""
    rprint("\n[bold blue]Qwen OAuth Login[/bold blue]\n")

    # Generate PKCE challenge
    verifier, challenge = qwen_get_pkce_challenge()

    rprint("[dim]Requesting device authorization...[/dim]\n")

    try:
        device_response = await qwen_request_device_code(challenge)
    except Exception as e:
        rprint(f"[red]Failed to get device code: {e}[/red]")
        return

    # Show verification URL
    url = device_response.verification_uri_complete
    rprint(f"Please visit this URL to authorize:\n[link={url}]{url}[/link]\n")

    # Try to open browser
    try:
        webbrowser.open(url)
        rprint("[dim]Browser opened. Please complete authorization...[/dim]\n")
    except Exception:
        rprint("[dim]Could not open browser. Please visit the URL manually.[/dim]\n")

    # Define status callback for progress updates
    def status_callback(status: str) -> None:
        rprint(f"[dim]{status}[/dim]")

    rprint("[dim]Waiting for authorization (press Ctrl+C to cancel)...[/dim]\n")

    try:
        token = await qwen_poll_for_token(
            device_code=device_response.device_code,
            verifier=verifier,
            expires_in=device_response.expires_in,
            status_callback=status_callback,
        )
    except KeyboardInterrupt:
        rprint("\n[yellow]Login cancelled.[/yellow]")
        return
    except Exception as e:
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            rprint("\n[red]Authorization timed out. Please try again.[/red]")
        else:
            rprint(f"\n[red]Failed to get token: {e}[/red]")
        return

    # Save to config
    try:
        save_oauth_token("qwen", token)
        rprint("\n[bold green]Successfully logged in to Qwen![/bold green]")
        rprint(
            "[dim]You can now use Qwen models. Run 'qqcode' and use /model to select a model.[/dim]\n"
        )
    except Exception as e:
        rprint(f"[red]Failed to save token: {e}[/red]")


def login_qwen() -> None:
    """Run the Qwen OAuth login flow."""
    asyncio.run(_login_qwen_async())
