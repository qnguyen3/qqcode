from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from vibe.core.oauth.base import AuthInteraction

CANCEL_MESSAGE = "Login cancelled"


def _html(title: str, message: str) -> bytes:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title></head><body>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p></body></html>"
    )
    return body.encode("utf-8")


def _http_response(status: int, body: bytes) -> bytes:
    reason = HTTPStatus(status).phrase
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    )
    return headers.encode("ascii") + body


@dataclass(frozen=True)
class CallbackOutcome[T]:
    """Result of racing the loopback callback against a manual code prompt."""

    from_callback: bool
    value: T | None = None
    input: str | None = None


class OAuthCallbackServer[T]:
    """Loopback OAuth redirect server used by browser sign-in flows."""

    def __init__(
        self,
        *,
        provider_name: str,
        host: str,
        port: int,
        path: str,
        complete: Callable[[str, Mapping[str, str]], Awaitable[T]],
        state: str | None,
        cancel_event: asyncio.Event | None,
        timeout_seconds: float | None,
        redirect_host: str | None,
    ) -> None:
        self.redirect_uri = ""
        self._provider_name = provider_name
        self._host = host
        self._port = port
        self._path = path
        self._complete = complete
        self._state = state
        self._cancel_event = cancel_event
        self._timeout_seconds = timeout_seconds
        self._redirect_host = redirect_host or host
        self._server: asyncio.AbstractServer | None = None
        self._claimed = False
        self._watchers: list[asyncio.Task[None]] = []
        self._future: asyncio.Future[T | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._future.add_done_callback(_consume_result)

    async def start(self) -> None:
        """Bind the loopback port and compute the redirect URI."""
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        port = int(self._server.sockets[0].getsockname()[1])
        host = self._redirect_host
        formatted = f"[{host}]" if ":" in host else host
        self.redirect_uri = f"http://{formatted}:{port}{self._path}"
        if self._cancel_event is not None:
            self._watchers.append(asyncio.create_task(self._watch_cancel()))
        if self._timeout_seconds is not None:
            self._watchers.append(asyncio.create_task(self._watch_timeout()))

    async def wait(self) -> T | None:
        """Wait for the browser redirect or a cancellation/timeout."""
        return await self._future

    def cancel(self) -> None:
        """Stop waiting unless a callback is already being completed."""
        if not self._claimed:
            self._finish(None)

    async def close(self) -> None:
        """Stop the server and release the loopback port."""
        self._finish_error(RuntimeError("OAuth callback server closed"))
        for watcher in self._watchers:
            watcher.cancel()
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()

    async def _watch_cancel(self) -> None:
        assert self._cancel_event is not None
        await self._cancel_event.wait()
        self._finish_error(RuntimeError(CANCEL_MESSAGE))

    async def _watch_timeout(self) -> None:
        assert self._timeout_seconds is not None
        await asyncio.sleep(self._timeout_seconds)
        self._finish_error(RuntimeError(f"{self._provider_name} sign-in timed out"))

    def _finish(self, value: T | None) -> None:
        if not self._future.done():
            self._future.set_result(value)

    def _finish_error(self, error: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(error)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            await _drain_headers(reader)
            status, body = await self._dispatch(request_line)
            writer.write(_http_response(status, body))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request_line: bytes) -> tuple[int, bytes]:
        try:
            method, target, *_ = request_line.decode("latin-1").split(" ")
        except ValueError:
            return HTTPStatus.BAD_REQUEST, _html("Bad request", "Malformed request.")
        parsed = urlparse(target)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        rejection = self._reject(method, parsed.path, params)
        if rejection is not None:
            return rejection
        return await self._complete_callback(params)

    def _reject(
        self, method: str, path: str, params: Mapping[str, str]
    ) -> tuple[int, bytes] | None:
        if method != "GET" or path != self._path:
            return HTTPStatus.NOT_FOUND, _html("Not found", "Callback route not found.")
        if self._state is not None and params.get("state") != self._state:
            return HTTPStatus.BAD_REQUEST, _html("State mismatch", "State mismatch.")
        if self._claimed or self._future.done():
            return HTTPStatus.CONFLICT, _html(
                "Already handled", "This sign-in has already been handled."
            )
        error = params.get("error")
        if error is None:
            return None
        description = params.get("error_description") or error
        self._finish_error(
            RuntimeError(f"{self._provider_name} authorization failed: {description}")
        )
        return HTTPStatus.BAD_REQUEST, _html(
            f"{self._provider_name} authorization failed", description
        )

    async def _complete_callback(self, params: Mapping[str, str]) -> tuple[int, bytes]:
        code = params.get("code")
        if not code:
            return HTTPStatus.BAD_REQUEST, _html(
                "Missing code", "Missing authorization code."
            )
        self._claimed = True
        try:
            value = await self._complete(code, params)
        except Exception as exc:
            self._finish_error(exc)
            return HTTPStatus.BAD_GATEWAY, _html(
                f"{self._provider_name} sign-in failed", str(exc)
            )
        self._finish(value)
        return HTTPStatus.OK, _html(
            "Signed in", f"Signed in to {self._provider_name}. You may close this page."
        )


def _consume_result(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


async def _drain_headers(reader: asyncio.StreamReader) -> None:
    while True:
        line = await reader.readline()
        if line in {b"\r\n", b"\n", b""}:
            return


async def start_oauth_callback_server[T](
    *,
    provider_name: str,
    host: str,
    port: int,
    path: str,
    complete: Callable[[str, Mapping[str, str]], Awaitable[T]],
    state: str | None = None,
    cancel_event: asyncio.Event | None = None,
    timeout_seconds: float | None = None,
    redirect_host: str | None = None,
) -> OAuthCallbackServer[T]:
    """Start a loopback callback server, returning it bound and listening."""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError(CANCEL_MESSAGE)
    server = OAuthCallbackServer(
        provider_name=provider_name,
        host=host,
        port=port,
        path=path,
        complete=complete,
        state=state,
        cancel_event=cancel_event,
        timeout_seconds=timeout_seconds,
        redirect_host=redirect_host,
    )
    await server.start()
    return server


async def wait_for_callback_or_manual_input[T](
    interaction: AuthInteraction,
    callback: OAuthCallbackServer[T] | None,
    *,
    message: str,
    placeholder: str,
) -> CallbackOutcome[T]:
    """Race the browser callback against a manual code/redirect-URL prompt."""
    if callback is None:
        return CallbackOutcome(
            from_callback=False,
            input=await interaction.prompt_text(message, placeholder=placeholder),
        )

    manual_task = asyncio.create_task(
        interaction.prompt_text(message, placeholder=placeholder)
    )
    callback_task = asyncio.create_task(callback.wait())
    try:
        done, _ = await asyncio.wait(
            {manual_task, callback_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if callback_task in done and (value := callback_task.result()) is not None:
            return CallbackOutcome(from_callback=True, value=value)
        if manual_task in done:
            callback.cancel()
            return CallbackOutcome(from_callback=False, input=manual_task.result())
        return CallbackOutcome(from_callback=False, input=await manual_task)
    finally:
        manual_task.cancel()
        callback_task.cancel()
        for task in (manual_task, callback_task):
            with suppress(asyncio.CancelledError):
                await task
