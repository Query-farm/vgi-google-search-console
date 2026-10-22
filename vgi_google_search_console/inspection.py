"""Google's index status for a URL: ``url_inspection()``.

``url_inspection()`` is a **blended**
(:class:`~vgi.table_in_out_function.RowTransformFunction`) table function. Its
positional arguments *are* the per-row input columns, so a single registration
serves both a literal call and a correlated LATERAL::

    SELECT verdict, coverage_state, last_crawl_time
    FROM gsc.main.url_inspection('sc-domain:example.com', 'https://example.com/pricing');

    SELECT p.page, i.verdict, i.coverage_state
    FROM gsc.main.search_analytics('sc-domain:example.com',
             dimensions => ['page'], max_rows => 20) p,
         LATERAL gsc.main.url_inspection(p.site_url, p.page) i;

Three constraints from the blended contract shape the signature:

* No ``finalize``/``finish`` — DuckDB forbids ``FinalExecute`` under correlated
  LATERAL, so nothing here may accumulate across batches.
* Positional args are read off ``batch`` by declared name; they are **not**
  surfaced on ``params.args``, which is why they are validated per row.
* A positional ``const`` arg is rejected, so every optional knob is a *named*
  arg instead.

Each inspection is one API call, and Google allows 2,000 a day per property.
That is the reason for ``cache_ttl``: under a LATERAL it turns a repeated URL
into a cache hit instead of another unit of quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.rpc import OutputCollector

from vgi_google_search_console import gsc_api
from vgi_google_search_console.common import REQUIRED_SECRETS, emit_fanout, session
from vgi_google_search_console.meta import docs, examples
from vgi_google_search_console.pushdown import build_filtered
from vgi_google_search_console.schemas import URL_INSPECTION_SCHEMA

#: The headline example: `search_analytics` supplies both input columns, so the
#: site never has to be written twice — or at all.
TOP_PAGE_SQL = (
    "SELECT p.page, i.verdict, i.coverage_state, i.last_crawl_time "
    "FROM (SELECT site_url, page FROM gsc.main.search_analytics('', "
    "dimensions => ['page'], max_rows => 100) ORDER BY clicks DESC LIMIT 1) p, "
    "LATERAL gsc.main.url_inspection(p.site_url, p.page) i"
)


def _strings(values: Any) -> list[str]:
    return [str(v) for v in values or [] if v is not None]


def flatten_inspection(site: str, url: str, result: dict[str, Any]) -> dict[str, Any]:
    """One ``inspectionResult`` as a row.

    The index, AMP and rich-results sections are merged into one row, since
    each is at most one object per URL. Rich results nest three levels deep —
    type, item, issue — and are flattened to a list of types plus one entry per
    issue, which is the level a question is actually asked at.
    """
    index = result.get("indexStatusResult") or {}
    amp = result.get("ampResult") or {}
    rich = result.get("richResultsResult") or {}
    detected = [item for item in rich.get("detectedItems") or [] if isinstance(item, dict)]
    rich_issues = [
        {
            "rich_result_type": kind.get("richResultType"),
            "item_name": item.get("name"),
            "severity": issue.get("severity"),
            "issue_message": issue.get("issueMessage"),
        }
        for kind in detected
        for item in kind.get("items") or []
        if isinstance(item, dict)
        for issue in item.get("issues") or []
        if isinstance(issue, dict)
    ]
    return {
        "site_url": site,
        "inspection_url": url,
        "verdict": index.get("verdict"),
        "coverage_state": index.get("coverageState"),
        "robots_txt_state": index.get("robotsTxtState"),
        "indexing_state": index.get("indexingState"),
        "page_fetch_state": index.get("pageFetchState"),
        "last_crawl_time": index.get("lastCrawlTime"),
        "crawled_as": index.get("crawledAs"),
        "google_canonical": index.get("googleCanonical"),
        "user_canonical": index.get("userCanonical"),
        "sitemaps": _strings(index.get("sitemap")),
        "referring_urls": _strings(index.get("referringUrls")),
        "amp_verdict": amp.get("verdict"),
        "amp_url": amp.get("ampUrl"),
        "amp_issues": [
            {"issue_message": issue.get("issueMessage"), "severity": issue.get("severity")}
            for issue in amp.get("issues") or []
            if isinstance(issue, dict)
        ],
        "rich_results_verdict": rich.get("verdict"),
        "rich_result_types": _strings(kind.get("richResultType") for kind in detected),
        "rich_result_issues": rich_issues,
        "inspection_result_link": result.get("inspectionResultLink"),
    }


@dataclass(slots=True, frozen=True, kw_only=True)
class UrlInspectionArgs:
    """``url_inspection(site_url, url)`` — both are per-row input columns."""

    site_url: Annotated[
        str,
        Arg(
            0,
            doc="Property the URL belongs to, e.g. 'sc-domain:example.com', exactly as the sites "
            "table lists it.",
        ),
    ]
    url: Annotated[
        str,
        Arg(1, doc="Full URL of the page to inspect, e.g. 'https://www.example.com/pricing'."),
    ]
    language_code: Annotated[
        str,
        Arg(
            "language_code",
            doc="BCP-47 language for the translated text in the result, such as coverage_state.",
            default="en-US",
            pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$",
        ),
    ] = "en-US"
    cache_ttl: Annotated[
        int,
        Arg(
            "cache_ttl",
            doc="Seconds to cache each URL's result (0 = off); saves quota when URLs repeat.",
            default=0,
            ge=0,
        ),
    ] = 0


class UrlInspectionFunction(RowTransformFunction[UrlInspectionArgs]):
    """What Google's index knows about one URL — 1->1 per non-null input row."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = URL_INSPECTION_SCHEMA

    class Meta:
        name = "url_inspection"
        description = "Google's index status for a URL: verdict, coverage, crawl, canonical, rich results"
        categories = ["indexing", "blended"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        tags = docs(
            category="indexing",
            result_schema=URL_INSPECTION_SCHEMA,
            llm=(
                "Whether one URL is in Google's index and why: the overall verdict, the coverage "
                "reason, when it was last crawled, whether robots.txt or a noindex blocks it, "
                "which canonical Google chose, and any AMP or structured-data problems. Use it to "
                "answer 'is this page indexed?' or 'why is this page not showing up?'. Takes the "
                "site_url and the page's full URL, and works under LATERAL to inspect many pages — "
                "but each row is one API call against a quota of 2,000 a day per property, so "
                "inspect a bounded list and pass cache_ttl. It reports what Google already knows; "
                "it does not fetch the page live or request indexing."
            ),
            md=(
                "The URL Inspection API's `index.inspect`, one row per URL.\n\n"
                "### Reading the result\n\n"
                "`verdict` is the summary — `PASS` means indexed — and `coverage_state` is the "
                "reason in words, such as 'Crawled - currently not indexed'. The three `*_state` "
                "columns say what stood in the way: `robots_txt_state` (crawling), "
                "`indexing_state` (a `noindex`) and `page_fetch_state` (the fetch itself).\n\n"
                "### Canonicals\n\n"
                "`user_canonical` is what the page declares; `google_canonical` is what Google "
                "chose. When they differ, Google decided another URL is the representative one, "
                "and that URL is the one that ranks.\n\n"
                "### Many URLs at once\n\n"
                "Both arguments are input columns, so a `LATERAL` join inspects one URL per "
                "driving row — and the performance function returns both, so its `site_url` and "
                "`page` columns feed this directly. A URL that is not part of the property is an "
                "error from Google, not an empty row; a NULL input produces no row.\n\n"
                "### Quota and caching\n\n"
                "Google allows 2,000 inspections a day and 600 a minute per property. Nothing is "
                "cached by default; `cache_ttl => N` caches each URL's result for N seconds, so a "
                "repeated URL — or a re-run of the same query — costs no quota.\n\n"
                "### Not a live test\n\n"
                "This reads the indexed version of the page. It does not crawl the page now, and "
                "this worker cannot request indexing: it is read-only."
            ),
            example_queries=examples(
                (
                    "Is the most-clicked page indexed, and when was it last crawled?",
                    TOP_PAGE_SQL,
                ),
                (
                    "Top pages where Google chose a different canonical than the page declares",
                    "SELECT p.page, i.user_canonical, i.google_canonical "
                    "FROM (SELECT site_url, page FROM gsc.main.search_analytics('', "
                    "dimensions => ['page'], max_rows => 100) ORDER BY clicks DESC LIMIT 10) p, "
                    "LATERAL gsc.main.url_inspection(p.site_url, p.page, cache_ttl => 3600) i "
                    "WHERE i.google_canonical IS DISTINCT FROM i.user_canonical",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=TOP_PAGE_SQL,
                description="Is the most-clicked page indexed, and when was it last crawled?",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[UrlInspectionArgs]) -> BindResponse:
        """Declare the fixed schema; the positional arguments are validated per row."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[UrlInspectionArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Inspect each input row's URL, mapping every output row back to its input row."""
        sites = batch.column("site_url").to_pylist()
        urls = batch.column("url").to_pylist()
        rows: list[dict[str, Any]] = []
        parents: list[int] = []
        sess = session(params)
        for index, (site, url) in enumerate(zip(sites, urls, strict=True)):
            if site is None or url is None:
                continue
            result = gsc_api.inspect_url(
                str(site), str(url), sess, language_code=params.args.language_code.strip()
            )
            rows.append(flatten_inspection(str(site).strip(), str(url).strip(), result))
            parents.append(index)
        built, kept = build_filtered(params, rows, parents)
        emit_fanout(out, built, kept, params.args.cache_ttl)


INDEXING_FUNCTIONS: list[type] = [UrlInspectionFunction]
