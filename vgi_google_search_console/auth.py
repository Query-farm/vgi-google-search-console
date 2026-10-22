"""Google credentials for the Search Console API: one secret type, three shapes, one fallback.

Every Search Console call needs an OAuth 2.0 bearer token carrying the
``webmasters.readonly`` scope — there is no public tier. Credentials arrive as a
DuckDB secret rather than an ATTACH option, because ATTACH option strings are
visible in ``duckdb_databases()`` and in anything that logs the statement, and
every shape below contains a long-lived credential::

    -- A service account added to the Search Console property as a user (recommended)
    CREATE SECRET gsc (TYPE google_search_console,
        service_account_json '{"type": "service_account", ...}');

    -- A user's OAuth client + refresh token (no service account needed)
    CREATE SECRET gsc (TYPE google_search_console,
        client_id '...apps.googleusercontent.com',
        client_secret '...', refresh_token '1//...');

    -- A short-lived access token minted elsewhere (testing, CI)
    CREATE SECRET gsc (TYPE google_search_console, access_token 'ya29....');

Every credential-bearing field is marked redacted, so ``duckdb_secrets()``
shows it masked.

When no secret resolves the worker falls back to Google's Application Default
Credentials (``GOOGLE_APPLICATION_CREDENTIALS``, or ``gcloud auth
application-default login --scopes=...``), which is what a local stdio worker
usually wants. A shared HTTP deployment must not hand its own identity to
every client, so the HTTP server turns that fallback off unless its operator
starts it with ``--allow-adc`` — see :func:`permit_adc`. Per catalog, the
``credentials => 'secret'`` ATTACH option turns it off too.

Only the read-only scope is ever requested. That is a second, independent
guarantee next to the request allow-list in :mod:`vgi_google_search_console.gsc_api`:
even a credential with edit rights is narrowed to read before it is used.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import google.auth
import pyarrow as pa
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2 import credentials as oauth2
from google.oauth2 import service_account
from vgi.catalog.secret_type import SecretTypeSpec

#: The DuckDB secret type this worker registers at ATTACH.
SECRET_TYPE = "google_search_console"

#: The one OAuth scope this worker ever asks for.
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

#: Google's OAuth token endpoint, used for the refresh-token grant.
TOKEN_URI = "https://oauth2.googleapis.com/token"

SERVICE_ACCOUNT_JSON = "service_account_json"
CLIENT_ID = "client_id"
CLIENT_SECRET = "client_secret"
REFRESH_TOKEN = "refresh_token"
ACCESS_TOKEN = "access_token"
QUOTA_PROJECT_ID = "quota_project_id"


_REDACT = {"redact": "true"}

SECRET_SPEC = SecretTypeSpec(
    name=SECRET_TYPE,
    description=(
        "Google Search Console credentials (scope webmasters.readonly). Provide ONE of: "
        "service_account_json (a service-account key, the account added to the Search Console "
        "property as a user); client_id + client_secret + refresh_token (an OAuth user grant); or "
        "access_token (short-lived). quota_project_id optionally bills API quota to a "
        "specific Google Cloud project."
    ),
    schema=pa.schema(
        [
            pa.field(SERVICE_ACCOUNT_JSON, pa.string(), metadata=_REDACT),
            pa.field(CLIENT_ID, pa.string()),
            pa.field(CLIENT_SECRET, pa.string(), metadata=_REDACT),
            pa.field(REFRESH_TOKEN, pa.string(), metadata=_REDACT),
            pa.field(ACCESS_TOKEN, pa.string(), metadata=_REDACT),
            pa.field(QUOTA_PROJECT_ID, pa.string()),
        ]
    ),
)


class SearchConsoleAuthError(RuntimeError):
    """No usable credential, or one that was supplied but cannot be used."""


#: ``google.auth.transport.Request``-compatible callable; supplied by ``gsc_api``
#: so the token endpoint is reached through the same HTTP client as the API.
RefreshRequest = Callable[..., Any]


@dataclass(slots=True)
class Credentials:
    """A resolved credential that can produce ``Authorization`` headers.

    Wraps a ``google.auth`` credentials object so that token refresh,
    expiry tracking and the JWT/refresh-token grants stay Google's code, not
    ours. ``source`` names where the credential came from, for error messages.
    """

    google: Any
    source: str
    lock: threading.Lock

    def headers(self, refresh: RefreshRequest) -> dict[str, str]:
        """Request headers carrying a valid bearer token, refreshing if needed.

        Args:
            refresh: A ``google.auth.transport.Request``-compatible callable used
                to reach the token endpoint when the cached token has expired.

        Returns:
            The ``Authorization`` header, plus ``x-goog-user-project`` when a
            quota project is set.

        Raises:
            SearchConsoleAuthError: The token endpoint refused the credential
                (revoked refresh token, deleted service-account key, ...).
        """
        with self.lock:
            if not self.google.valid:
                try:
                    self.google.refresh(refresh)
                except Exception as exc:  # google.auth.exceptions.RefreshError and transport errors
                    raise SearchConsoleAuthError(
                        f"could not obtain a Google access token from the {self.source}: {exc}"
                    ) from exc
            headers = {"authorization": f"Bearer {self.google.token}"}
            quota_project = getattr(self.google, "quota_project_id", None)
            if quota_project:
                headers["x-goog-user-project"] = str(quota_project)
            return headers


# ---------------------------------------------------------------------------
# Secret parsing
# ---------------------------------------------------------------------------


def _text(values: Mapping[str, Any], key: str) -> str:
    """One secret field as a stripped string; resolved secrets arrive as Arrow scalars."""
    raw = values.get(key)
    if raw is None:
        return ""
    value = raw.as_py() if hasattr(raw, "as_py") else raw
    return "" if value is None else str(value).strip()


def secret_values(secrets: Any) -> dict[str, Any] | None:
    """The first resolved ``google_search_console`` secret, whatever it was named.

    The framework keys resolved secrets by their DuckDB *name*, not their type,
    so ``CREATE SECRET my_ga (TYPE google_search_console, ...)`` arrives under
    ``my_ga``. Looking it up by type is the only lookup that works for every
    name a user might choose. Accepts the bind-time ``SecretsAccessor``, the
    process-time ``ResolvedSecrets`` dict, or a plain dict (tests).
    """
    if secrets is None:
        return None
    resolved = secrets.to_dict() if hasattr(secrets, "to_dict") else secrets
    if hasattr(resolved, "of_type"):
        matches = resolved.of_type(SECRET_TYPE)
        if matches:
            return dict(matches[0])
    if isinstance(resolved, Mapping):
        for name, values in resolved.items():
            if not isinstance(values, Mapping):
                continue
            if name == SECRET_TYPE or _text(values, "type") == SECRET_TYPE:
                return dict(values)
    return None


def _fingerprint(values: Mapping[str, Any]) -> str:
    """A stable hash of the credential fields, so equal secrets share one token."""
    keys = (SERVICE_ACCOUNT_JSON, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN, QUOTA_PROJECT_ID)
    material = "\x00".join(_text(values, key) for key in keys)
    return hashlib.sha256(material.encode()).hexdigest()


def _from_service_account(raw: str, quota_project: str) -> Any:
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearchConsoleAuthError(
            f"{SERVICE_ACCOUNT_JSON!r} in the {SECRET_TYPE!r} secret is not valid JSON "
            f"(paste the whole key file): {exc.msg}"
        ) from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        kind = info.get("type") if isinstance(info, dict) else type(info).__name__
        raise SearchConsoleAuthError(
            f"{SERVICE_ACCOUNT_JSON!r} must be a service-account key "
            f'("type": "service_account"), got {kind!r}. For an OAuth user grant use '
            f"{CLIENT_ID!r}, {CLIENT_SECRET!r} and {REFRESH_TOKEN!r} instead."
        )
    try:
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=[SCOPE]
        )
    except (ValueError, KeyError) as exc:
        raise SearchConsoleAuthError(f"the service-account key could not be loaded: {exc}") from exc
    if quota_project:
        credentials = credentials.with_quota_project(quota_project)
    return credentials


def _from_user_grant(client_id: str, client_secret: str, refresh_token: str, quota_project: str) -> Any:
    missing = [
        name
        for name, value in (
            (CLIENT_ID, client_id),
            (CLIENT_SECRET, client_secret),
            (REFRESH_TOKEN, refresh_token),
        )
        if not value
    ]
    if missing:
        raise SearchConsoleAuthError(
            f"an OAuth user grant needs {CLIENT_ID!r}, {CLIENT_SECRET!r} and {REFRESH_TOKEN!r}; "
            f"the {SECRET_TYPE!r} secret is missing {', '.join(repr(m) for m in missing)}"
        )
    return oauth2.Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=[SCOPE],
        quota_project_id=quota_project or None,
    )


def _from_access_token(token: str, quota_project: str) -> Any:
    # No refresh token and no expiry: google-auth treats it as valid until the
    # API says otherwise, which surfaces as a clear 401 from the chokepoint.
    return oauth2.Credentials(token=token, quota_project_id=quota_project or None)  # type: ignore[no-untyped-call]


def load(values: Mapping[str, Any]) -> Credentials:
    """Build credentials from one resolved secret's fields.

    Exactly one shape must be present. Supplying two is rejected rather than
    resolved by precedence: a secret carrying both a service-account key and a
    refresh token is a configuration mistake, and silently picking one would
    query the sites of whichever identity won.

    Raises:
        SearchConsoleAuthError: No shape, more than one shape, or a shape
            whose contents cannot be parsed.
    """
    service_json = _text(values, SERVICE_ACCOUNT_JSON)
    refresh_token = _text(values, REFRESH_TOKEN)
    access_token = _text(values, ACCESS_TOKEN)
    quota_project = _text(values, QUOTA_PROJECT_ID)
    shapes = [
        name
        for name, value in (
            (SERVICE_ACCOUNT_JSON, service_json),
            (REFRESH_TOKEN, refresh_token),
            (ACCESS_TOKEN, access_token),
        )
        if value
    ]
    if not shapes:
        raise SearchConsoleAuthError(
            f"the {SECRET_TYPE!r} secret carries no credential: set {SERVICE_ACCOUNT_JSON!r}, "
            f"or {CLIENT_ID!r} + {CLIENT_SECRET!r} + {REFRESH_TOKEN!r}, or {ACCESS_TOKEN!r}"
        )
    if len(shapes) > 1:
        raise SearchConsoleAuthError(
            f"the {SECRET_TYPE!r} secret sets {' and '.join(repr(s) for s in shapes)}; "
            "set exactly one kind of credential so it is unambiguous which identity queries Search Console"
        )
    if service_json:
        google = _from_service_account(service_json, quota_project)
        source = "service account in the google_search_console secret"
    elif refresh_token:
        google = _from_user_grant(
            _text(values, CLIENT_ID), _text(values, CLIENT_SECRET), refresh_token, quota_project
        )
        source = "OAuth refresh token in the google_search_console secret"
    else:
        google = _from_access_token(access_token, quota_project)
        source = "access token in the google_search_console secret"
    return Credentials(google=google, source=source, lock=threading.Lock())


# ---------------------------------------------------------------------------
# Resolution, with a per-process token cache
# ---------------------------------------------------------------------------

_cache: dict[str, Credentials] = {}
_cache_lock = threading.Lock()
_ADC_KEY = "<application-default-credentials>"


#: The operator's ceiling on the ADC fallback, set once at process start.
_operator_permits_adc = True


def permit_adc(allowed: bool) -> None:
    """Set whether this *process* may ever fall back to Application Default Credentials.

    A stdio worker runs as the user who attached it, so their own ADC is
    exactly the identity they meant, and the default is to permit it. An HTTP
    worker's ADC is the *server's* identity, so ``main_http`` forbids it unless
    the operator passes ``--allow-adc``. This is deliberately a process-level
    switch and not an ATTACH option: on a shared server, ATTACH options are
    chosen by clients, and a client must not be able to opt into the server's
    identity.
    """
    global _operator_permits_adc  # noqa: PLW0603 - a process-wide operator decision
    _operator_permits_adc = allowed


def operator_permits_adc() -> bool:
    """Whether :func:`permit_adc` currently allows the fallback."""
    return _operator_permits_adc


def _application_default() -> Credentials:
    try:
        found, _project = google.auth.default(scopes=[SCOPE])
    except DefaultCredentialsError as exc:
        raise SearchConsoleAuthError(_NO_CREDENTIALS) from exc
    return Credentials(google=found, source="Application Default Credentials", lock=threading.Lock())


_NO_CREDENTIALS = (
    "no Google Search Console credentials: CREATE SECRET (TYPE google_search_console, "
    "service_account_json '<key file contents>') — or client_id/client_secret/refresh_token — "
    "first. Locally you can instead ATTACH with credentials 'oauth' for browser sign-in, "
    "or run `gcloud auth application-default login --client-id-file=<desktop-client.json> "
    f"--scopes={SCOPE}`."
)


def resolve(secrets: Any, *, allow_adc: bool = True) -> Credentials:
    """The credentials for one call: the secret if present, else ADC if allowed.

    ADC is used only when both the caller (``allow_adc``, from the catalog's
    ATTACH options) and the operator (:func:`permit_adc`) allow it.

    Credentials are cached per process by a hash of their contents, so a paged
    scan or a burst of calls reuses one access token instead of minting a new
    one per request.

    Raises:
        SearchConsoleAuthError: Nothing resolved, or the secret is unusable.
            A secret that is *present* but broken never falls through to ADC —
            that would quietly query as a different identity.
    """
    values = secret_values(secrets)
    if values is not None:
        key = _fingerprint(values)
        with _cache_lock:
            cached = _cache.get(key)
            if cached is None:
                cached = _cache[key] = load(values)
            return cached
    if not (allow_adc and _operator_permits_adc):
        raise SearchConsoleAuthError(
            _NO_CREDENTIALS.split(" Locally", maxsplit=1)[0]
            + " (this worker does not fall back to its own Application Default Credentials)"
        )
    with _cache_lock:
        cached = _cache.get(_ADC_KEY)
        if cached is None:
            cached = _cache[_ADC_KEY] = _application_default()
        return cached


def reset_cache() -> None:
    """Forget every cached credential (tests, or to force a fresh token)."""
    with _cache_lock:
        _cache.clear()
