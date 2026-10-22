"""Small pieces every function module shares."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from vgi.arguments import SecretLookupEntry
from vgi.cache_control import CacheControl

from vgi_google_search_console import auth, oauth
from vgi_google_search_console.gsc_api import Session
from vgi_google_search_console.settings import OAUTH, Settings

if TYPE_CHECKING:
    import pyarrow as pa
    from vgi.protocol import VgiOutputCollector
    from vgi_rpc.rpc import OutputCollector

#: Every function asks the extension to resolve the ``google_search_console`` secret.
REQUIRED_SECRETS = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]

#: Grace window for serving a stale cached result when a refetch fails.
STALE_IF_ERROR = 300

#: The site and sitemap lists change when a human edits them in Search Console
#: — rarely, and never in a way a few minutes of staleness would mislead.
LISTING_TTL_SECONDS = 300


def session(params: Any) -> Session:
    """This call's credentials and its catalog's ATTACH settings.

    Settings arrive as the catalog's attach bytes; credentials come from the
    attachment's portable OAuth grant, the call's secrets, or Application Default
    Credentials when the settings and the operator both allow it.
    """
    settings = Settings.decode(getattr(params, "attach_opaque_data", None))
    if settings.credentials == OAUTH:
        creds = oauth.credentials(settings.oauth_grant)
    else:
        creds = auth.resolve(getattr(params, "secrets", None), allow_adc=settings.may_use_adc)
    return Session(credentials=creds, settings=settings)


def cache_for(ttl: int, *, per_value: bool = False) -> CacheControl | None:
    """Result-cache metadata for ``ttl`` seconds, or ``None`` for "do not cache"."""
    if ttl <= 0:
        return None
    return CacheControl(ttl=ttl, stale_if_error=STALE_IF_ERROR, per_value=per_value)


def emit(out: OutputCollector, batch: pa.RecordBatch, ttl: int = 0) -> None:
    """Emit ``batch``, advertising it as cacheable for ``ttl`` seconds when positive.

    The collector the framework passes is VGI's, whose ``emit`` takes
    ``cache_control``; it is typed as vgi-rpc's narrower base class.
    """
    cast("VgiOutputCollector", out).emit(batch, cache_control=cache_for(ttl))


def emit_fanout(
    out: OutputCollector, batch: pa.RecordBatch, parent_rows: Sequence[int], ttl: int = 0
) -> None:
    """Emit a batch produced from a batch of input rows, with its provenance.

    ``parent_rows[i]`` is the index, within the call's input batch, of the row
    that produced output row ``i``. Without it a correlated ``LATERAL`` cannot
    stamp the driving row's columns onto the right output rows. A positive
    ``ttl`` also turns on per-value memoization, so a repeated input value is
    answered from the cache rather than from the API.
    """
    cast("VgiOutputCollector", out).emit(
        batch, parent_rows=list(parent_rows), cache_control=cache_for(ttl, per_value=True)
    )
