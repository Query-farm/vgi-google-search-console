"""Per-attachment settings, chosen with ATTACH options.

Everything that changes how one attached catalog behaves is an ATTACH option,
not an environment variable — visible in the statement that made the choice,
and scoped to that one catalog::

    ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py',
                  credentials 'secret', page_size 5000);

The options are validated in ``catalog_attach`` and travel to every function as
the catalog's attach bytes (``params.attach_opaque_data``), which is the only
route an ATTACH-time choice has into a function body.

Credential options are security-relevant, because on a shared HTTP worker ATTACH
options are chosen by the *client*:

* ``credentials => 'auto'`` may fall back to Application Default Credentials —
  but only when the operator allows it (always for a stdio worker, which runs
  as the user who attached it; only with ``--allow-adc`` for the HTTP server).
* Overriding ``api_url`` disables that fallback outright, so no client can
  point the worker at its own host and receive the worker's own token.
* ``credentials => 'oauth'`` starts a local, in-memory Google login, explicitly
  enabled only by the stdio entry point and only with Google's API endpoint.
  The attach bytes carry the refreshable OAuth grant and must be treated as
  credentials by the client. No worker process owns the lifetime of the grant.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import pyarrow as pa
from vgi.catalog.attach_option import AttachOptionSpec

#: One host serves both halves of the API: the Search Analytics, Sites and
#: Sitemaps resources under ``/webmasters/v3`` and URL Inspection under ``/v1``.
DEFAULT_API_URL = "https://searchconsole.googleapis.com"

#: ``searchAnalytics.query`` returns at most 25,000 rows per request, and that
#: is also the default page: a query's cost is set by its date range and
#: grouping, not by how it is paged, so a smaller page only adds round trips.
MAX_PAGE_SIZE = 25_000
DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE

AUTO = "auto"
SECRET_ONLY = "secret"
OAUTH = "oauth"
CREDENTIAL_MODES = (AUTO, SECRET_ONLY, OAUTH)


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """Refreshable authorization carried privately in a client's attach bytes.

    The versioned payload has no configurable scope or token endpoint. The
    random attachment ID isolates credential caches even when Google returns
    the same refresh token for two authorizations. Reprs never expose secrets.
    """

    attachment_id: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    version: int = 1

    @classmethod
    def decode(cls, raw: Any) -> OAuthGrant:
        """Validate an opaque grant without including its contents in errors."""
        fields = {"version", "attachment_id", "client_id", "client_secret", "refresh_token"}
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or type(raw.get("version")) is not int
            or raw["version"] != 1
            or any(not isinstance(raw[key], str) or not raw[key].strip() for key in fields - {"version"})
            or not re.fullmatch(r"[0-9a-f]{32}", raw["attachment_id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", raw["client_id"])
        ):
            raise ValueError("Invalid OAuth grant in attachment; detach and attach again to sign in")
        return cls(**raw)


OPTION_SPECS = [
    AttachOptionSpec(
        name="credentials",
        desc=(
            "Where credentials come from: 'auto' (default) uses the google_search_console secret "
            "and otherwise Application Default Credentials where the worker permits it; 'secret' "
            "requires the secret and never falls back; 'oauth' signs in locally through the browser "
            "to Query Farm's app and carries refreshable credentials in the client's opaque attach data."
        ),
        type=pa.string(),
        default=AUTO,
    ),
    AttachOptionSpec(
        name="page_size",
        desc="Rows fetched per search_analytics() API request, up to 25000 (the default).",
        type=pa.int64(),
        default=DEFAULT_PAGE_SIZE,
    ),
    AttachOptionSpec(
        name="api_url",
        desc=(
            "Base URL of the Search Console API, for a proxy or a test double. Overriding it "
            "disables the Application Default Credentials fallback."
        ),
        type=pa.string(),
        default=DEFAULT_API_URL,
    ),
]


@dataclass(frozen=True, slots=True)
class Settings:
    """The validated ATTACH options of one catalog."""

    credentials: str = AUTO
    page_size: int = DEFAULT_PAGE_SIZE
    api_url: str = DEFAULT_API_URL
    # Internal credentials, never an ATTACH option or part of a diagnostic repr.
    oauth_grant: OAuthGrant | None = field(default=None, repr=False)

    @property
    def custom_endpoint(self) -> bool:
        """Whether the API URL was overridden."""
        return self.api_url != DEFAULT_API_URL

    @property
    def may_use_adc(self) -> bool:
        """Whether these settings permit the ADC fallback (the operator must also allow it)."""
        return self.credentials == AUTO and not self.custom_endpoint

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> Settings:
        """Validate raw ATTACH options.

        Raises:
            ValueError: An unknown credentials mode, an out-of-range page size, or
                an API URL that is not a plain http(s) base URL. Rejected at
                ATTACH rather than ignored, because every way of getting these
                wrong is otherwise silent.
        """
        mode = str(options.get("credentials") or AUTO).strip().lower()
        if mode not in CREDENTIAL_MODES:
            raise ValueError(
                f"ATTACH option credentials => {mode!r} is not one of {', '.join(CREDENTIAL_MODES)}"
            )
        raw_size = options.get("page_size")
        size = DEFAULT_PAGE_SIZE if raw_size is None else int(raw_size)
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(f"ATTACH option page_size => {size} must be between 1 and {MAX_PAGE_SIZE}")
        endpoint = _base_url("api_url", options.get("api_url") or DEFAULT_API_URL)
        if "oauth_profile" in options or "oauth_reauth" in options:
            raise ValueError("OAuth profiles were removed; each ATTACH now signs in independently")
        if mode == OAUTH and endpoint != DEFAULT_API_URL:
            raise ValueError("OAuth login requires the standard Google Search Console API endpoint")
        return cls(
            credentials=mode,
            page_size=size,
            api_url=endpoint,
        )

    def encode(self) -> bytes:
        """These settings as attach bytes; OAuth bytes contain sensitive credentials."""
        return json.dumps(asdict(self), sort_keys=True).encode()

    @classmethod
    def decode(cls, data: bytes | None) -> Settings:
        """Settings from attach bytes; defaults when there are none.

        No bytes means no ATTACH (a direct call, a test). Bytes that do not
        parse mean an older client; both get the defaults, which are the
        conservative choice — the real Google endpoint.
        """
        if not data:
            return cls()
        try:
            raw = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        try:
            decoded = cls.from_options(raw)
        except (TypeError, ValueError):
            if str(raw.get("credentials")).strip().lower() == OAUTH:
                raise ValueError("Invalid OAuth attachment; detach and attach again") from None
            return cls()
        if decoded.credentials == OAUTH:
            decoded = replace(decoded, oauth_grant=OAuthGrant.decode(raw.get("oauth_grant")))
        return decoded


def _base_url(name: str, value: Any) -> str:
    url = str(value).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError(f"ATTACH option {name} => {value!r} must be an http(s) base URL without a query")
    return url
