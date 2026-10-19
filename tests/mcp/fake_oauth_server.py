from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

RESOURCE_METADATA = "/.well-known/oauth-protected-resource"


class FakeOAuthServer:
    """Minimal OAuth 2.1 + DCR + PKCE server for tests (loopback only)."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, str] = {}
        self.valid_tokens: set[str] = {"access-1"}
        self.refresh_count = 0
        self.authorize_count = 0
        self.token_exchange_count = 0
        self.pkce_verified: bool | None = None
        self.last_code_verifier: str | None = None

        outer = self

        class _Handler(_FakeOAuthHandler):
            app = outer

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = Thread(
            target=self._httpd.serve_forever, daemon=True, name="fake-oauth"
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _issue_tokens(self) -> dict[str, Any]:
        with self._lock:
            self.token_exchange_count += 1
            index = self.token_exchange_count
            tokens = {
                "access_token": f"access-{index + 1}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": f"refresh-{index}",
            }
            self.valid_tokens.add(tokens["access_token"])
            return tokens


class _FakeOAuthHandler(BaseHTTPRequestHandler):
    app: ClassVar[FakeOAuthServer]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {key: values[0] for key, values in parse_qs(raw).items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        match parsed.path:
            case path if path.startswith(RESOURCE_METADATA):
                self._send_json({
                    "resource": self.app.mcp_url,
                    "authorization_servers": [self.app.base_url],
                })
            case "/.well-known/oauth-authorization-server":
                self._send_json({
                    "issuer": self.app.base_url,
                    "authorization_endpoint": f"{self.app.base_url}/authorize",
                    "token_endpoint": f"{self.app.base_url}/token",
                    "registration_endpoint": f"{self.app.base_url}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                })
            case "/authorize":
                params = parse_qs(parsed.query)
                state = (params.get("state") or [""])[0]
                challenge = (params.get("code_challenge") or [""])[0]
                redirect_uri = (params.get("redirect_uri") or [""])[0]
                with self.app._lock:
                    self.app.pending[state] = challenge
                    self.app.authorize_count += 1
                location = f"{redirect_uri}?code=test-code&state={state}"
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
            case "/mcp":
                auth = self.headers.get("Authorization", "")
                token = auth.removeprefix("Bearer ").strip()
                if token not in self.app.valid_tokens:
                    self._send_json(
                        {"error": "unauthorized"},
                        status=401,
                        headers={
                            "WWW-Authenticate": (
                                f'Bearer resource_metadata="{self.app.base_url}'
                                f'{RESOURCE_METADATA}"'
                            )
                        },
                    )
                    return
                self._send_json({"ok": True})
            case _:
                self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        match parsed.path:
            case "/register":
                data = self._read_json()
                client_id = f"fake-client-{len(self.app.clients) + 1}"
                response = {
                    **data,
                    "client_id": client_id,
                    "token_endpoint_auth_method": "none",
                }
                with self.app._lock:
                    self.app.clients[client_id] = response
                self._send_json(response, status=201)
            case "/token":
                form = self._read_form()
                if form.get("grant_type") == "refresh_token":
                    with self.app._lock:
                        self.app.refresh_count += 1
                    self._send_json({
                        "access_token": f"access-refresh-{self.app.refresh_count}",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": f"refresh-refresh-{self.app.refresh_count}",
                    })
                    return
                state = None
                with self.app._lock:
                    for key, challenge in self.app.pending.items():
                        if challenge:
                            state = key
                            break
                    expected = self.app.pending.pop(state, None) if state else None
                verifier = form.get("code_verifier", "")
                computed = (
                    urlsafe_b64encode(sha256(verifier.encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                with self.app._lock:
                    self.app.last_code_verifier = verifier
                    self.app.pkce_verified = bool(expected and computed == expected)
                if not self.app.pkce_verified:
                    self._send_json({"error": "invalid_grant"}, status=400)
                    return
                self._send_json(self.app._issue_tokens())
            case _:
                self.send_error(404)


def run_in_background(action: Callable[[], Any]) -> Thread:
    thread = Thread(target=action, daemon=True)
    thread.start()
    return thread
