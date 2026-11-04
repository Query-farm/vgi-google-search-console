"""Shared loopback OAuth listener with fixed, self-contained browser responses."""

from __future__ import annotations

import hmac
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from .oauth_page import CONTENT_SECURITY_POLICY, Outcome, Service, render_page


class OAuthCallback(HTTPServer):
    """A short-lived loopback listener for a desktop OAuth authorization code.

    It accepts only the expected callback path, Host and random state. It never
    logs the URL, which contains the authorization code, or reflects input in
    the browser response. The caller owns it with a ``with`` statement.
    """

    allow_reuse_address = False

    def __init__(self, state: str, service: Service) -> None:
        self.service = service
        self.state = state
        self.code = ""
        self.denied = False
        super().__init__(("127.0.0.1", 0), _OAuthCallbackHandler)
        self.timeout = 0.25

    @property
    def redirect_uri(self) -> str:
        """The loopback URL registered with this authorization request."""
        return f"http://127.0.0.1:{self.server_port}/oauth/callback"

    def get_request(self) -> tuple[socket.socket, Any]:
        """Bound how long an incomplete HTTP request can hold the listener."""
        connection, address = super().get_request()
        connection.settimeout(1.0)
        return connection, address

    def wait(self, timeout: float) -> str:
        """Wait for one matching response, or report denial / timeout."""
        deadline = time.monotonic() + timeout
        while not (self.code or self.denied):
            if time.monotonic() >= deadline:
                raise TimeoutError("Google sign-in timed out; run ATTACH again to retry")
            self.handle_request()
        if self.denied:
            raise ValueError("Google sign-in was not approved; run ATTACH again to retry")
        return self.code


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Keep callback URLs and authorization codes out of logs."""

    def do_GET(self) -> None:
        """Accept a code only when the path, host and state match this flow."""
        server = cast(OAuthCallback, self.server)
        try:
            parts = urlsplit(self.path)
            values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            self._respond(400, "invalid")
            return
        states = values.get("state", [])
        expected_host = f"127.0.0.1:{server.server_port}"
        valid = (
            parts.path == "/oauth/callback"
            and not parts.scheme
            and not parts.netloc
            and self.headers.get("Host") == expected_host
            and len(states) == 1
            and hmac.compare_digest(states[0].encode(), server.state.encode())
        )
        codes, errors = values.get("code", []), values.get("error", [])
        if valid and len(codes) == 1 and codes[0] and not errors:
            server.code = codes[0]
            self._respond(200, "received")
        elif valid and len(errors) == 1 and errors[0] and not codes:
            server.denied = True
            self._respond(200, "denied")
        else:
            self._respond(400, "invalid")

    def _respond(self, status: int, outcome: Outcome) -> None:
        body = render_page(cast(OAuthCallback, self.server).service, outcome)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
