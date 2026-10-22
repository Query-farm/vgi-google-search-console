"""Search performance as a table function: ``search_analytics()``.

``search_analytics()`` is the API's ``searchAnalytics.query`` — the data behind
the Performance report. Pick the dimensions to break the numbers down by and a
date range, and get one row per distinct combination with its clicks,
impressions, click-through rate and average position::

    SELECT date, query, clicks, impressions, position
    FROM gsc.main.search_analytics('sc-domain:example.com',
        dimensions => ['date', 'query'],
        start_date => '28daysAgo', end_date => 'today')
    ORDER BY clicks DESC;

The result schema follows the requested dimensions and is known at bind time
without a network call, so columns are typed (``DATE``, ``BIGINT``, ``DOUBLE``)
and a misspelled dimension fails at bind with the valid names. Rows stream one
API page per ``process()`` tick, so a ``LIMIT`` stops early and a large result
never sits in memory whole.

This is a paging scan rather than a blended row transform, for the reason
``vgi-kalshi`` gives for its own paged endpoints: a blended function must emit
everything for its input in a single ``process()`` call, so it would have to
walk every page before DuckDB saw a row. The price is that a scan takes
literal arguments only — no correlated ``LATERAL``, and no subquery for the
site. So the site argument also accepts ``''``, meaning every property the
credential can read, with a ``site_url`` column saying which row is whose;
several properties are then one query, and ``WHERE site_url = ...`` narrows it.

The aggregation happens at Google, not in DuckDB — this matters. Grouped by
``query``, rare queries are withheld for privacy and appear in no row at all, so
the rows do not add up to the property's total. Ask for the breakdown you want
to read: for a total, request no dimensions.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from vgi_google_search_console import gsc_api
from vgi_google_search_console import meta as vgi_meta
from vgi_google_search_console.common import REQUIRED_SECRETS, emit, session
from vgi_google_search_console.meta import docs, examples
from vgi_google_search_console.pushdown import build_filtered, date_bounds, dimension_filters
from vgi_google_search_console.schemas import TIMESTAMP
from vgi_google_search_console.sites import site_targets

_log = logging.getLogger(__name__)

#: Dates this function accepts: ISO dates, 'today', 'yesterday', or 'NdaysAgo'.
#: Declared on the arguments too, so agents can read the accepted shape from the
#: catalog. The API itself only takes ISO dates; the rest are resolved here.
DATE_PATTERN = r"^(\d{4}-\d{2}-\d{2}|today|yesterday|\d+daysAgo)$"
_DATE = re.compile(DATE_PATTERN)
_DAYS_AGO = re.compile(r"^(\d+)daysAgo$")
_DATE_DOC = "an ISO date, today, yesterday, or NdaysAgo (such as 28daysAgo), in Pacific Time"

#: Search Console buckets days in Pacific Time, whatever the site's audience.
_PACIFIC = "America/Los_Angeles"

#: Result column → (the API's name for the dimension, Arrow type, column comment).
DIMENSIONS: dict[str, tuple[str, pa.DataType, str]] = {
    "date": (
        "date",
        pa.date32(),
        "Day the searches happened, in Pacific Time (America/Los_Angeles) — how Search Console "
        "buckets days, whatever the site's own time zone.",
    ),
    "hour": (
        "hour",
        TIMESTAMP,
        "Start of the hour the searches happened in. Only available with data_state => "
        "'hourly_all', and only for the last several days.",
    ),
    "query": (
        "query",
        pa.string(),
        "The search query, lowercased by Google. Rare queries are withheld for privacy and "
        "appear in no row, so rows grouped by query do not add up to the property total.",
    ),
    "page": (
        "page",
        pa.string(),
        "URL of the page that appeared in the results, after any redirect.",
    ),
    "country": (
        "country",
        pa.string(),
        "Country the search came from, as a lowercase ISO 3166-1 alpha-3 code such as 'usa' or "
        "'deu' ('zzz' when unknown).",
    ),
    "device": ("device", pa.string(), "Device the search was made on: DESKTOP, MOBILE or TABLET."),
    "search_appearance": (
        "searchAppearance",
        pa.string(),
        "Search result feature the page was shown with, e.g. a rich result or video type.",
    ),
}

SITE_FIELD = vgi_meta.field(
    "site_url",
    pa.string(),
    "Property the row belongs to: the site_url argument, or each readable property in turn when "
    "the argument is ''.",
)

METRIC_FIELDS = (
    vgi_meta.field(
        "clicks",
        pa.int64(),
        "Clicks from Google results through to the property. Additive across rows.",
    ),
    vgi_meta.field(
        "impressions",
        pa.int64(),
        "Times a link to the property appeared in results a user saw. Additive across rows.",
    ),
    vgi_meta.field(
        "ctr",
        pa.float64(),
        "Click-through rate, clicks / impressions, between 0 and 1. Not additive: when combining "
        "rows, recompute it as sum(clicks) / sum(impressions) rather than averaging.",
    ),
    vgi_meta.field(
        "position",
        pa.float64(),
        "Average position of the property's topmost result, 1 being the top. Not additive: when "
        "combining rows, weight by impressions. NULL for Discover and Google News, which have no "
        "ranking.",
    ),
)

#: SQL-facing spellings → the API's.
SEARCH_TYPES = {
    "web": "web",
    "image": "image",
    "video": "video",
    "news": "news",
    "discover": "discover",
    "google_news": "googleNews",
}
AGGREGATION_TYPES = {
    "auto": "auto",
    "by_page": "byPage",
    "by_property": "byProperty",
    "by_news_showcase_panel": "byNewsShowcasePanel",
}
DATA_STATES = ("final", "all", "hourly_all")

#: Filter operators, keyed by their spelling with case and underscores removed,
#: so both the API's ``notContains`` and SQL-style ``not_contains`` are accepted.
_OPERATORS = {
    "equals": "equals",
    "notequals": "notEquals",
    "contains": "contains",
    "notcontains": "notContains",
    "includingregex": "includingRegex",
    "excludingregex": "excludingRegex",
}

#: Dimensions the API can filter on. ``date`` and ``hour`` are narrowed with the
#: date range instead.
_FILTERABLE = ("country", "device", "page", "query", "search_appearance")


def _squash(name: str) -> str:
    return name.strip().lower().replace("_", "")


_DIMENSION_BY_SPELLING = {_squash(column): column for column in DIMENSIONS}


def _list_arg(name: str, doc: str) -> Arg[list[str]]:
    return Arg(name, doc=doc, default=[], arrow_type=pa.list_(pa.string()))


# ---------------------------------------------------------------------------
# Validation and request building
# ---------------------------------------------------------------------------


def pacific_today() -> date:
    """Today's date where Search Console keeps its calendar."""
    try:
        return datetime.now(ZoneInfo(_PACIFIC)).date()
    except ZoneInfoNotFoundError:  # no tz database installed; standard time is close enough
        return (datetime.now(UTC) - timedelta(hours=8)).date()


def resolve_date(name: str, value: str, today: date) -> date:
    """Turn a date argument into a calendar day.

    Raises:
        ValueError: The value is not one of the accepted forms.
    """
    cleaned = value.strip()
    if cleaned == "today":
        return today
    if cleaned == "yesterday":
        return today - timedelta(days=1)
    if match := _DAYS_AGO.match(cleaned):
        return today - timedelta(days=int(match.group(1)))
    try:
        if not _DATE.match(cleaned):
            raise ValueError(cleaned)
        return date.fromisoformat(cleaned)
    except ValueError:
        raise ValueError(
            f"{name} => {value!r} is not a date: use 'YYYY-MM-DD', 'today', 'yesterday' or "
            "'NdaysAgo' (e.g. '28daysAgo')"
        ) from None


def normalise_dimensions(requested: Sequence[str]) -> list[str]:
    """The result column for each requested dimension, in order.

    Accepts the column spelling (``search_appearance``) and the API's
    (``searchAppearance``).

    Raises:
        ValueError: An unknown dimension, a metric passed as a dimension, or a
            dimension requested twice.
    """
    columns: list[str] = []
    for name in requested:
        column = _DIMENSION_BY_SPELLING.get(_squash(name or ""))
        if column is None:
            if _squash(name or "") in {f.name for f in METRIC_FIELDS}:
                raise ValueError(
                    f"{name!r} is a metric, not a dimension; clicks, impressions, ctr and position "
                    "are always returned"
                )
            raise ValueError(
                f"{name!r} is not a Search Console dimension; choose from {', '.join(DIMENSIONS)}"
            )
        if column in columns:
            raise ValueError(f"{name!r} is requested twice; each dimension becomes one column")
        columns.append(column)
    return columns


def parse_filters(raw: str) -> list[dict[str, str]]:
    """Parse the ``filters`` argument into the API's filter objects.

    The argument is a JSON array of ``{"dimension", "operator", "expression"}``
    objects (a single object is accepted too); ``operator`` defaults to
    ``equals``. They are ANDed together, which is all the API supports.

    Raises:
        ValueError: Invalid JSON, or a filter with an unknown dimension or
            operator or without an expression.
    """
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"filters is not valid JSON: {exc.msg} at position {exc.pos}") from exc
    items = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(
            "filters must be a JSON array of objects such as "
            '[{"dimension": "country", "operator": "equals", "expression": "usa"}]'
        )
    filters: list[dict[str, str]] = []
    for item in items:
        column = _DIMENSION_BY_SPELLING.get(_squash(str(item.get("dimension") or "")))
        if column not in _FILTERABLE:
            raise ValueError(
                f"filters cannot filter on {item.get('dimension')!r}; choose from "
                f"{', '.join(_FILTERABLE)} (narrow dates with start_date and end_date)"
            )
        operator = _OPERATORS.get(_squash(str(item.get("operator") or "equals")))
        if operator is None:
            raise ValueError(
                f"filters has an unknown operator {item.get('operator')!r}; choose from "
                "equals, not_equals, contains, not_contains, including_regex, excluding_regex"
            )
        expression = item.get("expression")
        if not isinstance(expression, str) or not expression:
            raise ValueError(f"the filter on {column!r} needs a non-empty string 'expression'")
        filters.append({"dimension": DIMENSIONS[column][0], "operator": operator, "expression": expression})
    return filters


def search_analytics_schema(columns: Sequence[str]) -> pa.Schema:
    """The result schema: the site, one column per requested dimension, then the four metrics."""
    fields = [vgi_meta.field(name, DIMENSIONS[name][1], DIMENSIONS[name][2]) for name in columns]
    return pa.schema([SITE_FIELD, *fields, *METRIC_FIELDS])


@dataclass(slots=True, frozen=True, kw_only=True)
class SearchAnalyticsArgs:
    """Arguments of ``search_analytics()``; everything but the site is named."""

    site_url: Annotated[
        str,
        Arg(
            0,
            doc="Property, e.g. 'sc-domain:example.com' or 'https://www.example.com/', exactly as "
            "the sites table lists it. Empty string = every property the credential can read.",
        ),
    ]
    dimensions: Annotated[
        list[str],
        _list_arg(
            "dimensions",
            "Dimensions to group by, each becoming a column, in this order; empty for one total "
            "row. The function documentation lists the seven dimensions.",
        ),
    ] = field(default_factory=list)
    start_date: Annotated[
        str,
        Arg(
            "start_date",
            doc=f"First day of the range, inclusive — {_DATE_DOC}.",
            default="28daysAgo",
            pattern=DATE_PATTERN,
        ),
    ] = "28daysAgo"
    end_date: Annotated[
        str,
        Arg(
            "end_date",
            doc=f"Last day of the range, inclusive — {_DATE_DOC}.",
            default="today",
            pattern=DATE_PATTERN,
        ),
    ] = "today"
    search_type: Annotated[
        str,
        Arg(
            "search_type",
            doc="Which Google surface to report on: web (default), image, video, news, discover or "
            "google_news.",
            default="web",
            choices=list(SEARCH_TYPES),
        ),
    ] = "web"
    aggregation_type: Annotated[
        str,
        Arg(
            "aggregation_type",
            doc="How results are counted: by_property counts a search once however many of the "
            "site's pages appeared, by_page counts each page; auto (default) lets Google choose "
            "from the dimensions.",
            default="auto",
            choices=list(AGGREGATION_TYPES),
        ),
    ] = "auto"
    data_state: Annotated[
        str,
        Arg(
            "data_state",
            doc="final (default) returns only finalized days, which lag by two or three; all "
            "includes fresh days that may still change; hourly_all is required for the hour "
            "dimension.",
            default="final",
            choices=list(DATA_STATES),
        ),
    ] = "final"
    filters: Annotated[
        str,
        Arg(
            "filters",
            doc="JSON array of filter objects, each with a dimension, an operator and an "
            "expression, ANDed together and applied by Google before aggregation; may name "
            "dimensions that are not in the output. The function documentation shows the syntax.",
            default="",
        ),
    ] = ""
    max_rows: Annotated[
        int,
        Arg(
            "max_rows",
            doc="Stop after this many rows for each property (0 = every row available).",
            default=0,
            ge=0,
        ),
    ] = 0
    cache_ttl: Annotated[
        int,
        Arg("cache_ttl", doc="Seconds DuckDB may reuse this result (0 = always re-query).", default=0, ge=0),
    ] = 0


def build_request(
    args: SearchAnalyticsArgs,
    pushed: Sequence[dict[str, str]],
    today: date,
    *,
    first: date | None = None,
    last: date | None = None,
) -> dict[str, Any] | None:
    """The ``searchAnalytics.query`` body for these arguments, minus paging.

    Args:
        args: The function's arguments.
        pushed: Filters translated from the SQL ``WHERE``, added to the caller's.
        today: The day relative dates are resolved against.
        first: A lower bound on ``date`` from the ``WHERE``, which can only
            narrow the range the arguments set.
        last: The matching upper bound.

    Returns:
        The request body, or ``None`` when the bounds leave no day to ask for.

    Raises:
        ValueError: Any argument that cannot be turned into a valid request.
    """
    columns = normalise_dimensions(args.dimensions)
    start = resolve_date("start_date", args.start_date, today)
    end = resolve_date("end_date", args.end_date, today)
    if start > end:
        raise ValueError(f"start_date ({start}) is after end_date ({end})")
    if "hour" in columns and args.data_state != "hourly_all":
        raise ValueError(
            "the hour dimension needs data_state => 'hourly_all' (hourly data covers only the last "
            "several days, and is not final)"
        )
    if first is not None:
        start = max(start, first)
    if last is not None:
        end = min(end, last)
    if start > end:
        return None
    body: dict[str, Any] = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": [DIMENSIONS[name][0] for name in columns],
        "type": SEARCH_TYPES[args.search_type],
        "aggregationType": AGGREGATION_TYPES[args.aggregation_type],
        "dataState": args.data_state,
    }
    filters = [*parse_filters(args.filters), *pushed]
    if filters:
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
    return body


def analytics_rows(response: dict[str, Any], columns: Sequence[str], site: str) -> list[dict[str, Any]]:
    """Turn a ``searchAnalytics.query`` response into rows keyed by column name.

    Each API row carries its dimension values positionally in ``keys``, in the
    order the dimensions were requested. ``site`` is stamped on, since the
    response does not repeat the property it was asked about.
    """
    rows: list[dict[str, Any]] = []
    for raw in response.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {
            "site_url": site,
            "clicks": raw.get("clicks"),
            "impressions": raw.get("impressions"),
            "ctr": raw.get("ctr"),
            "position": raw.get("position"),
        }
        row.update(zip(columns, raw.get("keys") or [], strict=False))
        rows.append(row)
    return rows


@dataclass(kw_only=True)
class SearchAnalyticsState(ArrowSerializableDataclass):
    """Offset paging through one query, for each site in turn.

    ``request_json`` freezes the request built on the first tick, and
    ``targets_json`` the sites still to visit, head first. Pushed filters are
    refreshed before every tick, and an offset is only meaningful against the
    query that produced it — continuing with a different filter would silently
    skip or repeat rows. Both are strings because scan state must round-trip
    through Arrow.
    """

    started: bool = False
    done: bool = False
    targets_json: str = ""
    request_json: str = ""
    #: Offset within the head site, and rows emitted for it so far.
    start_row: int = 0
    emitted: int = 0


#: The shape of search_analytics(), which depends on its arguments.
COLUMNS_MD = (
    "`site_url` first, then one column per entry of the `dimensions` argument in the order "
    "given, then the four metrics.\n\n"
    "#### Always first\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `site_url` | VARCHAR | Property the row belongs to. |\n\n"
    "#### One column per requested dimension\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `date` | DATE | Day of the searches, in Pacific Time. |\n"
    "| `hour` | TIMESTAMP WITH TIME ZONE | Start of the hour (needs `data_state => 'hourly_all'`). |\n"
    "| `query` | VARCHAR | The search query, lowercased; rare queries are withheld. |\n"
    "| `page` | VARCHAR | URL of the page shown in results. |\n"
    "| `country` | VARCHAR | Lowercase ISO 3166-1 alpha-3 code, e.g. `usa`. |\n"
    "| `device` | VARCHAR | `DESKTOP`, `MOBILE` or `TABLET`. |\n"
    "| `search_appearance` | VARCHAR | Search result feature the page was shown with. |\n\n"
    "#### Always last\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `clicks` | BIGINT | Clicks through to the property. |\n"
    "| `impressions` | BIGINT | Times the property appeared in results. |\n"
    "| `ctr` | DOUBLE | clicks / impressions, 0 to 1. Do not average. |\n"
    "| `position` | DOUBLE | Average topmost position, 1 = top; NULL for Discover and Google News. |"
)

_FILTER_EXAMPLE = '[{"dimension": "country", "operator": "equals", "expression": "usa"}]'

_DOCS = docs(
    category="performance",
    extra={"vgi.result_dynamic_columns_md": COLUMNS_MD},
    llm=(
        "Google Search performance for a property: clicks, impressions, click-through rate and "
        "average position, one row per combination of the requested `dimensions` (date, hour, "
        "query, page, country, device, search_appearance) over a date range (default: the last "
        "28 days). This is the function for almost every SEO question — top queries, top pages, "
        "trends, rankings. Pass a site_url exactly as the `sites` table lists it, as a literal; "
        "or pass '' to cover every readable property at once, told apart by the `site_url` "
        "column. Google does the aggregation: with the query or page dimension, rows do not add "
        "up to the property total (rare queries are withheld; a search showing two pages counts "
        "under both), so for a total request no dimensions or only date. Never average ctr or "
        "position across rows — recompute ctr from summed clicks and impressions and weight "
        "position by impressions. Recent days are missing unless data_state => 'all'. To filter "
        "on a dimension you do not want as a column, use the `filters` JSON argument."
    ),
    md=(
        "The Search Console API's `searchAnalytics.query` — the Performance report — as a table.\n\n"
        "### Shape\n\n"
        "`site_url`, then one column per requested dimension in order, then `clicks`, "
        "`impressions`, `ctr` and `position`. With no dimensions there is a single total row "
        "per property.\n\n"
        "### One property or all of them\n\n"
        "The first argument is a property's `site_url`, written as a literal. Pass `''` to walk "
        "every property the credential can read; a `WHERE site_url = ...` or `IN (...)` then "
        "visits only the properties it names, and a property that refuses the request is "
        "skipped. The argument cannot be a subquery or a column from a `LATERAL` join — `''` "
        "is how one query covers several properties.\n\n"
        "### Dates\n\n"
        "`start_date` and `end_date` take `YYYY-MM-DD`, `today`, `yesterday` or `NdaysAgo`, "
        "inclusive, in Pacific Time — Search Console's calendar, whatever the site's audience. "
        "Data is kept for 16 months. With the default `data_state => 'final'` the most recent "
        "two or three days are not there yet; `'all'` includes them while they are still "
        "changing, and `'hourly_all'` is what the `hour` dimension requires.\n\n"
        "### Filtering\n\n"
        "A SQL `WHERE` with `=` on a `page`, `query`, `country` or `device` column is sent to "
        "Google, and range predicates on `date` narrow the date range, so fewer rows come back; "
        "every other predicate is applied by the worker. To filter on a dimension that is *not* "
        "a column — clicks by device for one country, say — pass `filters`, a JSON array ANDed "
        "together:\n\n"
        "```sql\n"
        f"filters => '{_FILTER_EXAMPLE}'\n"
        "```\n\n"
        "Operators are `equals`, `not_equals`, `contains`, `not_contains`, `including_regex` and "
        "`excluding_regex` (RE2 syntax); filterable dimensions are `country`, `device`, `page`, "
        "`query` and `search_appearance`.\n\n"
        "### Totals are Google's, not SQL's\n\n"
        "Grouped by `query`, rare queries are withheld to protect privacy and appear in no row, "
        "so `sum(clicks)` over the rows is less than the property's real total. Grouped by "
        "`page`, a search that showed two of the site's pages counts as an impression for both. "
        "And Google returns only the top rows for a day, sorted by clicks. Request the breakdown "
        "you intend to read: for a property total, pass no dimensions.\n\n"
        "### Ratios do not add\n\n"
        "`ctr` and `position` are averages. When combining rows, recompute "
        "`sum(clicks) / sum(impressions)` and `sum(position * impressions) / sum(impressions)` "
        "rather than taking `avg()`.\n\n"
        "### What Google restricts\n\n"
        "`search_appearance` has to be the only dimension in a query; to break a feature down "
        "further, filter on it instead. Discover and Google News (`search_type`) have no "
        "queries and no `position`. Google rejects those combinations with its own message.\n\n"
        "### Quota\n\n"
        "Queries grouped or filtered by `page` or `query` over long ranges are the expensive "
        "ones. `cache_ttl` lets DuckDB reuse an identical query's result, and `max_rows` bounds "
        "how far the walk goes for each property."
    ),
    example_queries=examples(
        (
            "Daily clicks and impressions for each property over the last 28 days",
            "SELECT site_url, date, clicks, impressions FROM gsc.main.search_analytics("
            "'', dimensions => ['date']) ORDER BY site_url, date",
        ),
        (
            "The queries that bring the most clicks",
            "SELECT site_url, query, clicks, impressions, round(position, 1) AS position "
            "FROM gsc.main.search_analytics('', dimensions => ['query'], max_rows => 1000) "
            "ORDER BY clicks DESC LIMIT 25",
        ),
        (
            "Pages with many impressions but a low click-through rate",
            "SELECT site_url, page, impressions, clicks, round(ctr * 100, 2) AS ctr_percent "
            "FROM gsc.main.search_analytics('', dimensions => ['page'], max_rows => 1000) "
            "WHERE impressions > 100 ORDER BY ctr LIMIT 25",
        ),
        (
            "Clicks by device, for searches from one country only",
            "SELECT site_url, device, clicks, impressions FROM gsc.main.search_analytics("
            f"'', dimensions => ['device'], filters => '{_FILTER_EXAMPLE}') "
            "ORDER BY site_url, clicks DESC",
        ),
        (
            "Each property's total for the last 7 days, as one row",
            "SELECT site_url, clicks, impressions, ctr, position FROM gsc.main.search_analytics("
            "'', start_date => '7daysAgo') ORDER BY clicks DESC",
        ),
    ),
)


@init_single_worker
class SearchAnalyticsFunction(TableFunctionGenerator[SearchAnalyticsArgs, SearchAnalyticsState]):
    """``searchAnalytics.query`` as an offset-paged scan with a bind-time schema."""

    class Meta:
        name = "search_analytics"
        description = "Google Search performance: clicks, impressions, CTR and position by dimension"
        categories = ["performance"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        #: Never `filters_exactly_applied`: only `=` on four dimensions and
        #: ranges on `date` reach Google, and every pushed predicate is also
        #: applied locally.
        filter_pushdown = True
        auto_apply_filters = True
        tags = _DOCS
        examples = [
            FunctionExample(
                sql=(
                    "SELECT site_url, date, clicks, impressions FROM gsc.main.search_analytics("
                    "'', dimensions => ['date']) ORDER BY site_url, date"
                ),
                description="Daily clicks and impressions for each property over the last 28 days",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[SearchAnalyticsArgs]) -> BindResponse:
        """Validate the whole request and declare its columns; no network is needed."""
        args = params.args
        if args.site_url.strip():
            gsc_api.site_url(args.site_url)
        build_request(args, [], pacific_today())  # dates, dimensions and filters fail here, not mid-scan
        return BindResponse(output_schema=search_analytics_schema(normalise_dimensions(args.dimensions)))

    @classmethod
    def initial_state(cls, params: ProcessParams[SearchAnalyticsArgs]) -> SearchAnalyticsState:
        """Start before the first site."""
        return SearchAnalyticsState()

    @classmethod
    def process(
        cls,
        params: ProcessParams[SearchAnalyticsArgs],
        state: SearchAnalyticsState,
        out: OutputCollector,
    ) -> None:
        """Fetch and emit one page of one site."""
        args = params.args
        if state.done:
            out.finish()
            return
        sess = session(params)
        named = args.site_url.strip()
        columns = normalise_dimensions(args.dimensions)
        if not state.started:
            first, last = date_bounds(params) if "date" in columns else (None, None)
            request = build_request(
                args, dimension_filters(params, columns), pacific_today(), first=first, last=last
            )
            # No request means the WHERE excludes every day in the range.
            targets = site_targets(params, sess, named) if request is not None else []
            state.started = True
            state.targets_json = json.dumps(targets)
            state.request_json = json.dumps(request)
        targets = json.loads(state.targets_json)
        if not targets:
            state.done = True
            out.finish()
            return
        site = targets[0]
        body: dict[str, Any] = json.loads(state.request_json)
        page = sess.settings.page_size
        if args.max_rows:
            page = min(page, args.max_rows - state.emitted)
        body["startRow"] = state.start_row
        body["rowLimit"] = page
        try:
            response = gsc_api.search_analytics(site, body, sess)
        except gsc_api.SearchConsoleError as exc:
            if named or not exc.is_access_refusal:
                raise
            _log.warning("skipping %s during an every-site walk: %s", site, exc)
            rows: list[dict[str, Any]] = []
        else:
            rows = analytics_rows(response, columns, site)
        state.start_row += len(rows)
        state.emitted += len(rows)
        # A site's walk ends on an empty page, which is the end Google
        # documents: a start row past the last result is answered with zero
        # rows. A page shorter than requested is deliberately *not* treated as
        # the end — stopping there would turn a short page mid-result into a
        # silently truncated answer, and the price of being careful is one
        # request. With no dimensions there is at most one row, so nothing to
        # probe for.
        site_done = not rows or not columns or (args.max_rows > 0 and state.emitted >= args.max_rows)
        if site_done:
            targets.pop(0)
            state.targets_json = json.dumps(targets)
            state.start_row = 0
            state.emitted = 0
            state.done = not targets
        batch, _ = build_filtered(params, rows)
        emit(out, batch, args.cache_ttl)


PERFORMANCE_FUNCTIONS: list[type] = [SearchAnalyticsFunction]
