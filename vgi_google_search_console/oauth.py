"""Local sign-in to Query Farm's Google OAuth app, with portable attach credentials.

The client retains each Google grant in its opaque attach data and sends it to
subsequent workers. Workers can exit or restart without losing authorization.
There is no credential file, OS store or long-lived loopback token server.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import re
import secrets
import sys
import threading
import webbrowser
from collections import OrderedDict
from typing import Any

from google.auth.exceptions import TransportError
from oauthlib.oauth2 import OAuth2Error, WebApplicationClient

from . import auth, oauth_app
from .oauth_adapter import AuthError, api
from .oauth_model import OAuthGrant

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
LOGIN_TIMEOUT = 180.0

_local_enabled = False
_CACHE_LIMIT = 128
_cache: OrderedDict[OAuthGrant, auth.Credentials] = OrderedDict()
_cache_lock = threading.Lock()


def permit_local(allowed: bool) -> None:
    """Enable browser sign-in only when the entry point is serving stdio."""
    global _local_enabled  # noqa: PLW0603 - operator decision, not an ATTACH option
    _local_enabled = allowed


def _require_local() -> None:
    if not _local_enabled:
        raise AuthError(
            "Browser OAuth is available only for a local stdio worker. "
            f"For a remote worker, supply your own {auth.SECRET_TYPE} secret."
        )


def _client() -> dict[str, str]:
    if (
        re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", oauth_app.CLIENT_ID)
        and oauth_app.CLIENT_SECRET
    ):
        return {auth.CLIENT_ID: oauth_app.CLIENT_ID, auth.CLIENT_SECRET: oauth_app.CLIENT_SECRET}
    raise AuthError(
        "Query Farm's Google OAuth app registration is not included in this build. "
        "Query Farm must register a Desktop app and set CLIENT_ID and CLIENT_SECRET "
        "in oauth_app.py before distributing the worker. End users do not need to create an app."
    )


def authorize() -> OAuthGrant:
    """Return this attachment's refreshable grant after a separate browser login."""
    _require_local()
    record = _browser_login(_client())
    return OAuthGrant.decode({**record, "version": 1, "attachment_id": secrets.token_hex(16)})


class _OAuthCredentials(auth.Credentials):
    def headers(self, refresh: auth.RefreshRequest) -> dict[str, str]:
        """Refresh without reflecting Google's response or grant into RPC errors."""
        try:
            return super().headers(refresh)
        except AuthError:
            raise AuthError(
                "Could not refresh the Google OAuth login; check the connection or detach and attach again"
            ) from None


def credentials(grant: OAuthGrant | None) -> auth.Credentials:
    """Reconstruct authorization in any local worker, reusing its cached access token."""
    _require_local()
    if grant is None:
        raise AuthError("Missing OAuth grant; detach and attach again to sign in")
    with _cache_lock:
        credential = _cache.get(grant)
        if credential is None:
            loaded = auth.load(
                {
                    auth.CLIENT_ID: grant.client_id,
                    auth.CLIENT_SECRET: grant.client_secret,
                    auth.REFRESH_TOKEN: grant.refresh_token,
                }
            )
            credential = _OAuthCredentials(
                google=loaded.google, source="Query Farm OAuth attachment", lock=loaded.lock
            )
            _cache[grant] = credential
            if len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
        _cache.move_to_end(grant)
        return credential


def forget(grant: OAuthGrant | None) -> None:
    """Evict this worker's cached credentials; the client owns the attachment data.

    This is not Google revocation and cannot invalidate copies held by another
    worker or client. Revocation remains a Google account operation.
    """
    _require_local()
    if grant is not None:
        with _cache_lock:
            _cache.pop(grant, None)


def clear_cache() -> None:
    """Drop this worker's credential cache without invalidating portable grants."""
    with _cache_lock:
        _cache.clear()


atexit.register(clear_cache)


def _browser_login(client: dict[str, str]) -> dict[str, str]:
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    flow = WebApplicationClient(client[auth.CLIENT_ID], scope=[auth.SCOPE])
    with api.OAuthCallback(state) as callback:
        url = flow.prepare_request_uri(
            AUTHORIZE_URL,
            redirect_uri=callback.redirect_uri,
            scope=[auth.SCOPE],
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
            access_type="offline",
            prompt="consent select_account",
        )
        print("Opening Google sign-in. Complete it in your browser to finish ATTACH.", file=sys.stderr)
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error:
            opened = False
        if not opened:
            print(f"Open this URL in a browser on this computer:\n{url}", file=sys.stderr)
        try:
            code = callback.wait(LOGIN_TIMEOUT)
        except (TimeoutError, ValueError) as exc:
            raise AuthError(str(exc)) from exc
        body = flow.prepare_request_body(
            code=code,
            redirect_uri=callback.redirect_uri,
            code_verifier=verifier,
            client_secret=client[auth.CLIENT_SECRET],
        )
        try:
            response = api.AuthTransport()(
                auth.TOKEN_URI,
                method="POST",
                body=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except TransportError as exc:
            raise AuthError(
                "Could not reach Google to finish sign-in; check the connection and retry ATTACH"
            ) from exc
    if response.status != 200:
        raise AuthError("Google could not complete sign-in; retry ATTACH")
    try:
        token: dict[str, Any] = flow.parse_request_body_response(response.data.decode())
    except (OAuth2Error, ValueError, Warning) as exc:
        raise AuthError("Google returned an invalid or incomplete authorization") from exc
    refresh = token.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError("Google did not grant offline access; retry and approve access")
    print("Google sign-in complete. This connection is ready.", file=sys.stderr)
    return {**client, auth.REFRESH_TOKEN: refresh}
