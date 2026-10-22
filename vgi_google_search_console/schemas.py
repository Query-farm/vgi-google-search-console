"""Arrow schemas for the fixed-shape functions, and total value coercion.

``search_analytics()`` builds its schema at bind time from the dimensions
requested (see :mod:`vgi_google_search_console.search_analytics`); everything
else here has one fixed shape, declared once with a comment per column. Those
comments are the single source of a column's documentation: DuckDB shows them
in ``DESCRIBE`` and ``duckdb_columns()``, and
:mod:`vgi_google_search_console.meta` reads them back to declare each function's
result schema for vgi-lint.

Every coercer is **total**: a value Arrow would reject becomes NULL instead of
raising, because one odd value in a 25,000-row page must not fail the page.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa

from vgi_google_search_console.meta import field

#: Instants: sitemap submission and download times, crawl times, and the
#: ``hour`` dimension, which Google sends with its Pacific-time offset.
TIMESTAMP = pa.timestamp("us", tz="UTC")

#: The window nanosecond-resolution clients (pandas, numpy) can hold; anything
#: outside it raises at materialization, which is worse than NULL.
_NS_FLOOR = datetime(1678, 1, 1, tzinfo=UTC)
_NS_CEILING = datetime(2262, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Total coercers
# ---------------------------------------------------------------------------


def to_int(value: Any) -> int | None:
    """An int64, from an int, an integral float or a numeric string; NULL otherwise.

    Google encodes int64 as a JSON *string* (``"1234"``) in the sitemap
    resource, and Search Analytics sends click and impression counts as JSON
    doubles (``12.0``); both are whole numbers and both arrive here.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    else:
        try:
            result = int(str(value))
        except ValueError:
            try:
                as_float = float(str(value))
            except ValueError:
                return None
            if not math.isfinite(as_float) or not as_float.is_integer():
                return None
            result = int(as_float)
    return result if -(2**63) <= result < 2**63 else None


def to_float(value: Any) -> float | None:
    """A double, from a number or numeric string; NULL for anything else or non-finite."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_bool(value: Any) -> bool | None:
    """A boolean, from a JSON bool or ``'true'``/``'false'``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def to_timestamp(value: Any) -> datetime | None:
    """An aware UTC datetime from an RFC 3339 string, or NULL."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed if _NS_FLOOR <= parsed < _NS_CEILING else None


def to_date(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` string as a DATE, or NULL."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _coerce(value: Any, kind: pa.DataType) -> Any:  # noqa: PLR0911 - one return per type family
    """Convert one payload value to what ``kind`` declares, or NULL."""
    if value is None:
        return None
    if pa.types.is_string(kind):
        return value if isinstance(value, str) else str(value)
    if pa.types.is_int64(kind):
        return to_int(value)
    if pa.types.is_float64(kind):
        return to_float(value)
    if pa.types.is_boolean(kind):
        return to_bool(value)
    if pa.types.is_date32(kind):
        return to_date(value)
    if pa.types.is_timestamp(kind):
        return to_timestamp(value)
    if pa.types.is_list(kind):
        if not isinstance(value, (list, tuple)):
            return None
        return [_coerce(item, kind.value_type) for item in value]
    if pa.types.is_struct(kind):
        if not isinstance(value, dict):
            return None
        return {f.name: _coerce(value.get(f.name), f.type) for f in kind}
    return value


def column(rows: Sequence[dict[str, Any]], name: str, kind: pa.DataType) -> pa.Array:
    """One column of ``rows`` as an Arrow array of ``kind``."""
    return pa.array([_coerce(row.get(name), kind) for row in rows], type=kind)


def batch_from_rows(rows: Sequence[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build one RecordBatch by pulling each schema field out of ``rows`` by name."""
    return pa.RecordBatch.from_arrays([column(rows, f.name, f.type) for f in schema], schema=schema)


# ---------------------------------------------------------------------------
# Sites and sitemaps
# ---------------------------------------------------------------------------

_SITE_URL = (
    "The property, exactly as Search Console lists it: 'sc-domain:example.com' for a domain "
    "property, or the full URL such as 'https://www.example.com/' for a URL-prefix property. "
    "The first argument every other function takes."
)

SITE_SCHEMA = pa.schema(
    [
        field("site_url", pa.string(), _SITE_URL),
        field(
            "permission_level",
            pa.string(),
            "What the credential may do with the property: siteOwner, siteFullUser, "
            "siteRestrictedUser, or siteUnverifiedUser (listed, but its data cannot be read).",
        ),
        field(
            "property_type",
            pa.string(),
            "'domain' for a domain property, which covers every protocol and subdomain, or "
            "'url_prefix' for one that covers only URLs beginning with site_url.",
        ),
    ]
)

SITEMAP_CONTENT = pa.struct(
    [
        ("type", pa.string()),
        ("submitted", pa.int64()),
        ("indexed", pa.int64()),
    ]
)

SITEMAP_SCHEMA = pa.schema(
    [
        field("site_url", pa.string(), "Property the sitemap was submitted for."),
        field("path", pa.string(), "URL of the sitemap."),
        field(
            "type",
            pa.string(),
            "Format Google detected: sitemap, rssFeed, atomFeed, urlList, patternSitemap or notSitemap.",
        ),
        field(
            "is_sitemaps_index",
            pa.bool_(),
            "True for a sitemap index, which lists other sitemaps rather than pages.",
        ),
        field(
            "is_pending",
            pa.bool_(),
            "True while Google has not yet processed the sitemap since it was submitted.",
        ),
        field("last_submitted", TIMESTAMP, "When the sitemap was last submitted to Search Console."),
        field("last_downloaded", TIMESTAMP, "When Google last fetched the sitemap."),
        field("warnings", pa.int64(), "Number of warnings Google raised for the sitemap."),
        field("errors", pa.int64(), "Number of errors Google found in the sitemap."),
        field(
            "contents",
            pa.list_(SITEMAP_CONTENT),
            "What the sitemap lists, one entry per content type (web, image, video, news, …) with "
            "the number of URLs submitted. `indexed` is deprecated by Google and usually NULL. "
            "Apply unnest() for one row per content type.",
        ),
    ]
)

# ---------------------------------------------------------------------------
# URL Inspection
# ---------------------------------------------------------------------------

AMP_ISSUE = pa.struct([("issue_message", pa.string()), ("severity", pa.string())])

RICH_RESULT_ISSUE = pa.struct(
    [
        ("rich_result_type", pa.string()),
        ("item_name", pa.string()),
        ("severity", pa.string()),
        ("issue_message", pa.string()),
    ]
)

URL_INSPECTION_SCHEMA = pa.schema(
    [
        field("site_url", pa.string(), "Property the URL was inspected under."),
        field("inspection_url", pa.string(), "The URL that was inspected."),
        field(
            "verdict",
            pa.string(),
            "Google's overall index verdict: PASS (indexed and eligible to appear), PARTIAL, FAIL "
            "(not indexed), or NEUTRAL (excluded, e.g. a duplicate or a redirect).",
        ),
        field(
            "coverage_state",
            pa.string(),
            "The reason behind the verdict, as the Search Console interface words it, e.g. "
            "'Submitted and indexed' or 'Crawled - currently not indexed'.",
        ),
        field(
            "robots_txt_state",
            pa.string(),
            "Whether robots.txt lets Google crawl the URL: ALLOWED or DISALLOWED.",
        ),
        field(
            "indexing_state",
            pa.string(),
            "Whether the page permits indexing: INDEXING_ALLOWED, BLOCKED_BY_META_TAG, "
            "BLOCKED_BY_HTTP_HEADER or BLOCKED_BY_ROBOTS_TXT.",
        ),
        field(
            "page_fetch_state",
            pa.string(),
            "Outcome of Google's last fetch: SUCCESSFUL, SOFT_404, NOT_FOUND, ACCESS_DENIED, "
            "ACCESS_FORBIDDEN, BLOCKED_ROBOTS_TXT, BLOCKED_4XX, SERVER_ERROR, REDIRECT_ERROR, "
            "INTERNAL_CRAWL_ERROR or INVALID_URL.",
        ),
        field(
            "last_crawl_time",
            TIMESTAMP,
            "When Google last crawled the URL; NULL if it never has.",
        ),
        field("crawled_as", pa.string(), "Which crawler fetched it: DESKTOP or MOBILE."),
        field(
            "google_canonical",
            pa.string(),
            "The URL Google chose as canonical — the one that is actually indexed and shown.",
        ),
        field(
            "user_canonical",
            pa.string(),
            "The canonical URL the page declares for itself; differs from google_canonical when "
            "Google overrode the declaration.",
        ),
        field("sitemaps", pa.list_(pa.string()), "Submitted sitemaps that list this URL."),
        field(
            "referring_urls",
            pa.list_(pa.string()),
            "Pages through which Google found this URL (a sample, not the full link graph).",
        ),
        field(
            "amp_verdict",
            pa.string(),
            "Verdict for the page's AMP version; NULL when it has none.",
        ),
        field("amp_url", pa.string(), "URL of the AMP version that was inspected."),
        field(
            "amp_issues",
            pa.list_(AMP_ISSUE),
            "Problems found in the AMP version, each with its message and severity (WARNING or ERROR).",
        ),
        field(
            "rich_results_verdict",
            pa.string(),
            "Verdict for the structured data on the page; NULL when none was detected.",
        ),
        field(
            "rich_result_types",
            pa.list_(pa.string()),
            "Rich result types detected on the page, e.g. 'Breadcrumbs', 'FAQ', 'Product snippets'.",
        ),
        field(
            "rich_result_issues",
            pa.list_(RICH_RESULT_ISSUE),
            "Problems in the structured data, one entry per issue with the rich result type and "
            "item it belongs to and its severity (WARNING or ERROR).",
        ),
        field(
            "inspection_result_link",
            pa.string(),
            "Link to this inspection in the Search Console interface.",
        ),
    ]
)
