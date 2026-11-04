"""Read-only HTTP access to the Google Search Console API.

This module is the **only** place in the package that makes upstream HTTP
requests or imports ``httpx``; ``tests/test_readonly_guard.py`` enforces this.
Every API request goes through :func:`_call`, which enforces an allow-list
rather than trusting call sites:

* ``GET`` — the site list and each site's sitemap list.
* ``POST`` — only to the two RPCs that *read*: ``searchAnalytics/query`` and
  ``urlInspection/index:inspect``. Google models those as POST because their
  request bodies are structured, not because they change anything.

Anything else — adding or removing a site, submitting or deleting a sitemap —
is refused before a socket is opened. Combined with the read-only OAuth scope
requested in :mod:`vgi_google_search_console.auth`, a write is impossible twice
over.

The API is spoken as REST+JSON rather than through Google's generated client:
one small dependency instead of the discovery client and its transport, plain
dicts that map straight onto Arrow, and an ``httpx.MockTransport`` seam that
lets the offline suite exercise the real request/response code.
"""

from __future__ import annotations

import atexit
import random
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from google.auth.exceptions import TransportError

from vgi_google_search_console.auth import Credentials
from vgi_google_search_console.oauth_callback import OAuthCallback as _OAuthCallback
from vgi_google_search_console.settings import MAX_PAGE_SIZE, Settings

__all__ = ["MAX_PAGE_SIZE", "SearchConsoleError", "Session"]

#: Path of the Sites, Sitemaps and Search Analytics resources below the base URL.
WEBMASTERS_PATH = "/webmasters/v3"

#: Path of the URL Inspection RPC below the base URL.
INSPECT_PATH = "/v1/urlInspection/index:inspect"

#: The one per-site RPC that is a read despite being a POST.
_SEARCH_ANALYTICS = re.compile(r"^/webmasters/v3/sites/[^/?#]+/searchAnalytics/query$")

#: Connect and read timeouts. A wide Search Analytics query grouped by page and
#: query can take tens of seconds to compute server-side.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

#: Statuses worth retrying: the per-minute rate limits, plus transient 5xx.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BASE_SECONDS = 1.0

#: The older half of the API reports a rate limit as 403 with one of these
#: reasons rather than as 429. They clear within the minute, so they retry too.
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

#: Reasons that mean a quota is spent rather than that access is missing.
_QUOTA_REASONS = _RATE_REASONS | {"quotaExceeded", "dailyLimitExceeded", "RESOURCE_EXHAUSTED"}

_DOMAIN_PREFIX = "sc-domain:"
_HOSTNAME = re.compile(r"^[^\s/:?#@]+\.[^\s/:?#@]+$")
_URL_PREFIX = re.compile(r"^https?://[^\s/?#]+(/\S*)?$")


class SearchConsoleError(RuntimeError):
    """A Search Console API call failed; the message is safe to show a user.

    Attributes:
        status: HTTP status code (0 for transport failures).
        reason: Google's error reason or canonical status, e.g. ``forbidden``
            or ``PERMISSION_DENIED``.
    """

    def __init__(self, message: str, *, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    @property
    def is_access_refusal(self) -> bool:
        """Whether this says "you may not read this property", and nothing broader.

        That is the one failure an every-property walk may step over: it
        concerns a single site, and the rest of the walk is unaffected. A spent
        quota or a disabled API also arrives as a 403, but it will fail every
        site that follows — skipping those would turn an outage into a result
        that looks merely empty.
        """
        if self.status not in {403, 404}:
            return False
        return self.reason not in _QUOTA_REASONS and self.reason != "SERVICE_DISABLED"


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def site_url(value: str) -> str:
    """Check that ``value`` names a Search Console property, and return it stripped.

    A property is identified by the exact string Search Console lists it under:
    ``sc-domain:example.com`` for a domain property, or the full URL with its
    scheme (``https://www.example.com/``) for a URL-prefix property. The string
    is otherwise passed through untouched — ``http`` and ``https``, and ``www``
    and bare hosts, are *different* properties, so nothing here normalises.

    Raises:
        ValueError: The value is neither form. A bare hostname gets a pointed
            message, since that is the common mistake.
    """
    raw = (value or "").strip()
    if raw.startswith(_DOMAIN_PREFIX):
        if _HOSTNAME.match(raw.removeprefix(_DOMAIN_PREFIX)):
            return raw
    elif _URL_PREFIX.match(raw):
        return raw
    if _HOSTNAME.match(raw):
        raise ValueError(
            f"{value!r} is a hostname, not a Search Console property: use 'sc-domain:{raw}' for a "
            f"domain property or 'https://{raw}/' for a URL-prefix property — list yours with "
            "gsc.main.sites"
        )
    raise ValueError(
        f"{value!r} is not a Search Console property (expected 'sc-domain:example.com' or a URL "
        "prefix such as 'https://www.example.com/'); list yours with gsc.main.sites"
    )


def page_url(value: str) -> str:
    """Check that ``value`` is an absolute http(s) URL to inspect, and return it stripped.

    Raises:
        ValueError: The value is not an absolute URL.
    """
    raw = (value or "").strip()
    if not _URL_PREFIX.match(raw):
        raise ValueError(
            f"{value!r} is not a URL to inspect: pass the page's full address, including the "
            "scheme, such as 'https://www.example.com/pricing'"
        )
    return raw


def segment(value: str) -> str:
    """Percent-encode one path segment, so a value cannot escape its position.

    A site URL reaches a request path straight from user SQL and is full of
    ``/``, ``:`` and sometimes ``?``. Encoded, it stays one segment whatever it
    contains — which is what lets the allow-list reason about the path's shape.
    """
    return quote(value, safe="")


def property_type(site: Any) -> str | None:
    """``'domain'`` or ``'url_prefix'`` for a site URL, by its form."""
    if not isinstance(site, str) or not site:
        return None
    return "domain" if site.startswith(_DOMAIN_PREFIX) else "url_prefix"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """Who is asking (``credentials``) and where to ask (``settings``), for one call."""

    credentials: Credentials
    settings: Settings = field(default_factory=Settings)


def open_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client carrying this module's timeouts; ``transport`` is the test seam."""
    return httpx.Client(timeout=TIMEOUT, transport=transport)


_shared: httpx.Client | None = None
_shared_lock = threading.Lock()
_transport_override: httpx.BaseTransport | None = None


def shared_client() -> httpx.Client:
    """The process-wide connection pool.

    A paged scan fetches one page per ``process()`` tick with no enclosing scope
    to hold a client open, so without a shared pool every page would pay a new
    TLS handshake.
    """
    global _shared  # noqa: PLW0603 - a module-level pool is the point
    if _shared is None or _shared.is_closed:
        with _shared_lock:
            if _shared is None or _shared.is_closed:
                _shared = open_client(_transport_override)
    return _shared


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Route every request through ``transport`` (tests); ``None`` restores the network."""
    global _transport_override  # noqa: PLW0603 - test seam
    _transport_override = transport
    reset_shared_client()


def reset_shared_client() -> None:
    """Close and forget the process pool, so the next call opens a fresh one."""
    global _shared  # noqa: PLW0603
    with _shared_lock:
        if _shared is not None:
            _shared.close()
        _shared = None


atexit.register(reset_shared_client)


@dataclass(slots=True)
class _AuthResponse:
    """The ``google.auth.transport.Response`` shape google-auth reads back."""

    status: int
    headers: Mapping[str, str]
    data: bytes


class AuthTransport:
    """``google.auth.transport.Request`` implemented on the shared httpx pool.

    google-auth mints and refreshes tokens by calling a transport object; its
    stock ones need ``requests``. This keeps token traffic on the same client,
    timeouts and test seam as the API traffic, and keeps ``httpx`` confined to
    this module.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_kwargs: Any,
    ) -> _AuthResponse:
        """Perform one token-endpoint (or metadata-server) request."""
        try:
            response = shared_client().request(
                method,
                url,
                content=body,
                headers=dict(headers or {}),
                timeout=timeout if timeout is not None else TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"token request to {url} failed: {exc}") from exc  # type: ignore[no-untyped-call]
        return _AuthResponse(status=response.status_code, headers=response.headers, data=response.content)


_AUTH_TRANSPORT = AuthTransport()


class OAuthCallback(_OAuthCallback):
    """Use the shared loopback listener with this connector's browser page."""

    def __init__(self, state: str) -> None:
        super().__init__(state, "google-search-console")


def _allowed(method: str, url: str, settings: Settings) -> bool:
    """The read-only allow-list: GET on the site resources, POST only to the two read RPCs."""
    base = settings.api_url
    if not url.startswith(base + "/"):
        return False
    path = url[len(base) :]
    if "?" in path or "#" in path:
        return False
    if method == "GET":
        return path == f"{WEBMASTERS_PATH}/sites" or path.startswith(f"{WEBMASTERS_PATH}/sites/")
    if method == "POST":
        return path == INSPECT_PATH or _SEARCH_ANALYTICS.match(path) is not None
    return False


def _reason_of(payload: Any) -> tuple[str, str]:
    """``(message, reason)`` from a Google error payload, in either of its two dialects."""
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return "", ""
    error = payload["error"]
    message = str(error.get("message") or "")
    reason = str(error.get("status") or "")
    # The webmasters half answers in the older dialect: no `status`, and the
    # machine-readable reason inside an `errors` list.
    for item in error.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reason = reason or str(item["reason"])
            if str(item["reason"]) in _QUOTA_REASONS:
                reason = str(item["reason"])
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason") == "SERVICE_DISABLED":
            reason = "SERVICE_DISABLED"
    return message, reason


def _error_from(response: httpx.Response, credentials: Credentials) -> SearchConsoleError:
    """Turn a Google error payload into a message an operator can act on."""
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None
    message, reason = _reason_of(payload)
    message = message or response.text[:500] or response.reason_phrase

    hint = ""
    if reason == "SERVICE_DISABLED" or "has not been used in project" in message:
        hint = (
            " Enable the 'Google Search Console API' in the Google Cloud project that owns this credential."
        )
    elif status == 429 or reason in _QUOTA_REASONS:
        hint = (
            " A Search Console API quota is exhausted. Search Analytics is limited per site and "
            "per user each minute, and by a load quota that queries grouped or filtered by page "
            "or query over long date ranges spend fastest; URL Inspection allows 2,000 calls a "
            "day per site. Narrow the date range or the grouping, or retry later."
        )
    elif status == 403:
        email = getattr(credentials.google, "service_account_email", None)
        who = f"the service account {email}" if email else "this identity"
        hint = (
            f" Add {who} as a user of the property in Search Console (Settings → Users and "
            "permissions), and check the exact site_url with gsc.main.sites — a domain property "
            "is 'sc-domain:example.com', a URL-prefix property is the full URL."
        )
    elif status == 401:
        hint = f" The {credentials.source} was rejected; it may be expired or revoked."
    return SearchConsoleError(
        f"Search Console API error {status}{f' {reason}' if reason else ''}: {message}{hint}",
        status=status,
        reason=reason,
    )


def _retryable(response: httpx.Response) -> bool:
    """Whether ``response`` is a transient failure worth another attempt."""
    if response.status_code in _RETRYABLE:
        return True
    if response.status_code != 403:
        return False
    try:
        _message, reason = _reason_of(response.json())
    except ValueError:
        return False
    return reason in _RATE_REASONS


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before retry ``attempt``: ``Retry-After`` if sent, else jittered backoff."""
    if response is not None:
        after = response.headers.get("retry-after", "")
        if after.isdigit():
            return min(float(after), 30.0)
    return float(_RETRY_BASE_SECONDS * (2**attempt) * (0.5 + random.random() / 2))


def _call(
    method: str,
    url: str,
    session: Session,
    *,
    params: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The single chokepoint: allow-list, authenticate, retry, decode.

    Raises:
        PermissionError: The request is not on the read-only allow-list. This is
            a programming error, never a user one.
        SearchConsoleError: The API refused the request or kept failing.
    """
    credentials = session.credentials
    if not _allowed(method, url, session.settings):
        raise PermissionError(f"refusing {method} {url}: not a read-only Search Console request")
    client = shared_client()
    last: httpx.Response | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        headers = credentials.headers(_AUTH_TRANSPORT)
        try:
            response = client.request(method, url, params=params, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_retry_delay(None, attempt))
                continue
            raise SearchConsoleError(f"Search Console API timed out: {method} {url}") from exc
        except httpx.HTTPError as exc:
            raise SearchConsoleError(f"could not reach the Search Console API: {exc}") from exc
        if attempt + 1 < _RETRY_ATTEMPTS and _retryable(response):
            last = response
            time.sleep(_retry_delay(response, attempt))
            continue
        if response.status_code >= 400:
            raise _error_from(response, credentials)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchConsoleError(
                f"Search Console returned a non-JSON {response.status_code} for {method} {url}: "
                f"{response.text[:200]!r}",
                status=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise SearchConsoleError(f"unexpected Search Console response shape for {url}")
        return payload
    assert last is not None  # the loop only falls through after a retryable response
    raise _error_from(last, credentials)


def _dicts(items: Any) -> list[dict[str, Any]]:
    return [item for item in items or [] if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Sites and sitemaps
# ---------------------------------------------------------------------------


def sites(session: Session) -> list[dict[str, Any]]:
    """Every property the credential has been added to — ``sites.list``.

    Unpaged on Google's side: the whole list comes back in one response.
    """
    payload = _call("GET", f"{session.settings.api_url}{WEBMASTERS_PATH}/sites", session)
    return _dicts(payload.get("siteEntry"))


def sitemaps(site: str, session: Session, *, sitemap_index: str = "") -> list[dict[str, Any]]:
    """The sitemaps submitted for ``site`` — ``sitemaps.list``, also unpaged.

    Args:
        site: The property, as Search Console lists it.
        session: Who to ask as, and where.
        sitemap_index: When given, list the sitemaps inside that sitemap index
            instead of the ones submitted directly.
    """
    url = f"{session.settings.api_url}{WEBMASTERS_PATH}/sites/{segment(site_url(site))}/sitemaps"
    params = {"sitemapIndex": sitemap_index} if sitemap_index else None
    return _dicts(_call("GET", url, session, params=params).get("sitemap"))


# ---------------------------------------------------------------------------
# Search Analytics
# ---------------------------------------------------------------------------


def search_analytics(site: str, body: Mapping[str, Any], session: Session) -> dict[str, Any]:
    """One page of ``searchAnalytics.query`` for ``site``."""
    url = f"{session.settings.api_url}{WEBMASTERS_PATH}/sites/{segment(site_url(site))}/searchAnalytics/query"
    return _call("POST", url, session, body=body)


# ---------------------------------------------------------------------------
# URL Inspection
# ---------------------------------------------------------------------------


def inspect_url(site: str, url: str, session: Session, *, language_code: str = "") -> dict[str, Any]:
    """Google's index status for one URL of ``site`` — ``urlInspection.index.inspect``.

    Returns the ``inspectionResult`` object. This reads what Google already
    knows about the URL; it is not a live test and does not request a crawl.
    """
    body: dict[str, Any] = {"inspectionUrl": page_url(url), "siteUrl": site_url(site)}
    if language_code:
        body["languageCode"] = language_code
    payload = _call("POST", f"{session.settings.api_url}{INSPECT_PATH}", session, body=body)
    result = payload.get("inspectionResult")
    return result if isinstance(result, dict) else {}
