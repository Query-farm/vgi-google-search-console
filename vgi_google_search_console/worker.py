"""VGI worker exposing Google Search Console to DuckDB/SQL (read-only).

    ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py');
    CREATE SECRET gsc (TYPE google_search_console, service_account_json '...');
    SELECT * FROM gsc.sites;
    SELECT query, clicks, impressions FROM gsc.search_analytics('sc-domain:example.com',
        dimensions => ['query']) ORDER BY clicks DESC;

Function names are bare (``search_analytics``, not ``gsc_search_analytics``)
because they are already qualified by the catalog they live in.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema
from vgi.catalog.catalog_interface import AttachOpaqueData, CatalogAttachResult, CatalogInfo
from vgi.catalog.descriptors import Table

from vgi_google_search_console import __version__, auth, oauth, settings
from vgi_google_search_console.inspection import INDEXING_FUNCTIONS, TOP_PAGE_SQL
from vgi_google_search_console.meta import column_comments, docs, examples, keywords
from vgi_google_search_console.schemas import SITE_SCHEMA
from vgi_google_search_console.search_analytics import PERFORMANCE_FUNCTIONS
from vgi_google_search_console.sites import SITE_FUNCTIONS, AllSitesFunction

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-google-search-console"

_FUNCTIONS = [*SITE_FUNCTIONS, *PERFORMANCE_FUNCTIONS, *INDEXING_FUNCTIONS]

_EXAMPLE_QUERIES = examples(
    (
        "Every property this credential can read",
        "SELECT site_url, property_type, permission_level FROM gsc.main.sites ORDER BY site_url",
    ),
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
        "Clicks by country and device",
        "SELECT site_url, country, device, clicks FROM gsc.main.search_analytics("
        "'', dimensions => ['country', 'device'], max_rows => 1000) ORDER BY clicks DESC LIMIT 25",
    ),
    (
        "Sitemaps with errors or warnings, across every property",
        "SELECT site_url, path, errors, warnings FROM gsc.main.sitemaps('') "
        "WHERE errors > 0 OR warnings > 0 ORDER BY errors DESC",
    ),
    ("Index status of the most-clicked page", TOP_PAGE_SQL),
)

#: Run by `vgi-lint --execute` against a live credential, so each must hold for
#: *any* account: no site literal, no assumption about traffic volume.
_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "sites_and_scan_function_agree",
            "description": "The sites table and the function behind it return the same rows.",
            "sql": (
                "SELECT (SELECT count(*) FROM gsc.main.sites) = (SELECT count(*) FROM gsc.main.all_sites())"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "property_types_are_derived",
            "description": "Every property is classified as a domain or a URL-prefix property.",
            "sql": (
                "SELECT bool_and(property_type IN ('domain', 'url_prefix')) IS NOT FALSE FROM gsc.main.sites"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "search_analytics_columns_are_typed",
            "description": "A date column is a DATE, counts are BIGINT and ratios DOUBLE.",
            "sql": (
                "SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
                "gsc.main.search_analytics('', dimensions => ['date'])) ORDER BY column_name"
            ),
            "expected_result": [
                ["clicks", "BIGINT"],
                ["ctr", "DOUBLE"],
                ["date", "DATE"],
                ["impressions", "BIGINT"],
                ["position", "DOUBLE"],
                ["site_url", "VARCHAR"],
            ],
        },
        {
            "name": "rows_belong_to_readable_properties",
            "description": "An every-property query only returns rows for properties in the sites table.",
            "sql": (
                "SELECT count(*) FROM gsc.main.search_analytics('', dimensions => ['date'], "
                "start_date => '7daysAgo') WHERE site_url NOT IN (SELECT site_url FROM gsc.main.sites)"
            ),
            "expected_result": [[0]],
        },
        {
            "name": "max_rows_caps_each_property",
            "description": "max_rows bounds the rows returned for each property.",
            "sql": (
                "SELECT coalesce(max(n), 0) <= 3 FROM (SELECT count(*) AS n FROM "
                "gsc.main.search_analytics('', dimensions => ['query'], max_rows => 3) GROUP BY site_url)"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "a_date_predicate_is_honoured",
            "description": "Rows outside a WHERE on date never come back.",
            "sql": (
                "SELECT count(*) FROM gsc.main.search_analytics('', dimensions => ['date']) "
                "WHERE date > current_date + 1"
            ),
            "expected_result": [[0]],
        },
    ]
)

#: The agent-suitability suite: questions an analyst should be able to answer
#: from the catalog metadata alone. Only the public half lives here; the graders
#: (reference SQL, success criteria) are in vgi-agent-tests.yaml, so an agent
#: under evaluation cannot read the answers off the catalog.
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "find_sites",
            "prompt": "Which Google Search Console properties can I query, and what is my access to each?",
        },
        {
            "name": "daily_clicks",
            "prompt": (
                "How many clicks and impressions did each of my Search Console properties get from "
                "Google Search on each of the last 7 days?"
            ),
        },
        {
            "name": "total_clicks_not_summed",
            "prompt": (
                "How many clicks in total did each of my properties get from Google Search in the "
                "last 28 days?"
            ),
        },
        {
            "name": "average_position",
            "prompt": (
                "What was the average position in Google Search of each of my properties over the "
                "last 28 days?"
            ),
        },
        {
            "name": "top_queries",
            "prompt": "Which search queries brought the most clicks to my properties in the last 28 days?",
        },
        {
            "name": "top_pages",
            "prompt": "Which pages across my properties got the most impressions in the last 28 days?",
        },
        {
            "name": "filter_on_hidden_dimension",
            "prompt": (
                "For searches made from the United States only, how do each property's clicks split "
                "between desktop, mobile and tablet?"
            ),
        },
        {
            "name": "fresh_data",
            "prompt": (
                "How many clicks did each of my properties get yesterday, even if the number is not final?"
            ),
        },
        {
            "name": "image_search",
            "prompt": "Which queries bring my properties the most clicks from Google Images?",
        },
        {
            "name": "sitemap_health",
            "prompt": (
                "Do any of the sitemaps submitted for my Search Console properties have errors or warnings?"
            ),
        },
        {
            "name": "sitemap_contents",
            "prompt": "How many URLs have my properties submitted in sitemaps, by content type?",
        },
        {
            "name": "is_it_indexed",
            "prompt": ("Is my most-clicked page indexed by Google, and when was it last crawled?"),
        },
        {
            "name": "sites_via_function",
            "prompt": (
                "How many of my Search Console properties are domain properties versus URL-prefix ones?"
            ),
        },
    ]
)

_CATEGORIES = json.dumps(
    [
        {
            "name": "discovery",
            "title": "Discovery",
            "description": (
                "Which properties you can read — where the first argument of everything else comes from."
            ),
            "keywords": ["sites", "properties", "permissions", "site url"],
        },
        {
            "name": "performance",
            "title": "Search Performance",
            "description": (
                "Clicks, impressions, click-through rate and position by query, page, country, "
                "device and date."
            ),
            "keywords": ["clicks", "impressions", "ctr", "position", "queries", "pages", "seo"],
        },
        {
            "name": "sitemaps",
            "title": "Sitemaps",
            "description": "Sitemaps submitted for a property, with their processing status and contents.",
            "keywords": ["sitemaps", "sitemap index", "errors", "warnings", "submitted urls"],
        },
        {
            "name": "indexing",
            "title": "Indexing",
            "description": (
                "Google's index status for individual URLs: verdict, crawl, canonical and rich results."
            ),
            "keywords": ["url inspection", "indexed", "crawl", "canonical", "robots.txt", "rich results"],
        },
    ]
)

_DOC_LLM = (
    "Google Search Console data for every property the configured Google credential can read. "
    "Use it for organic search (SEO) questions: which queries and pages get clicks and "
    "impressions from Google Search, how rankings and click-through rates move over time, by "
    "country and device; whether sitemaps are healthy; and whether a URL is indexed. Start with "
    "the `sites` table to get a site_url, and pass it exactly as listed, as a literal, as the "
    "first argument of `search_analytics()`, which takes a list of `dimensions`; pass '' "
    "instead to cover every property at once. Aggregation happens at Google: "
    "rows grouped by query or page do not add up to the property total, and ctr and position "
    "must not be averaged. Read-only; use local browser sign-in with ATTACH credentials 'oauth', "
    "a `google_search_console` secret, or permitted Application Default Credentials."
)

_CATALOG_TAGS = {
    "provider": "google-search-console",
    "domain": "search-analytics",
    "vgi.title": "Google Search Console",
    "vgi.source_url": SOURCE_URL,
    "vgi.author": "Query Farm LLC <hello@query.farm>",
    "vgi.copyright": (
        "Worker (c) 2026 Query Farm LLC - https://query.farm. Search data belongs to the owner "
        "of each property and is accessed under the Google APIs terms of service."
    ),
    "vgi.license": "MIT",
    "vgi.support_contact": "https://github.com/Query-farm/vgi-google-search-console/issues",
    "vgi.support_policy_url": "https://github.com/Query-farm/vgi-google-search-console/blob/main/README.md",
    "vgi.keywords": keywords(
        "google search console",
        "seo",
        "search analytics",
        "organic search",
        "clicks",
        "impressions",
        "rankings",
        "sitemaps",
        "url inspection",
    ),
    "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
    "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
    "vgi.doc_llm": _DOC_LLM,
    "vgi.doc_md": (
        "Google Search Console — how a site performs in Google Search — read through the Search "
        "Console API.\n\n"
        "### How the data is shaped\n\n"
        "- A **property** (a *site*) is the unit everything is reported on. It is either a "
        "*domain property*, written `sc-domain:example.com`, or a *URL-prefix property*, written "
        "as the full URL such as `https://www.example.com/`. They are different properties with "
        "different data, and the string must match exactly.\n"
        "- **Search performance** is clicks, impressions, click-through rate and position, broken "
        "down by the dimensions you ask for over a date range.\n"
        "- **Sitemaps** and **URL inspection** describe how Google crawls and indexes the "
        "property.\n\n"
        "### Getting started\n\n"
        "1. Take a `site_url` from the `sites` table.\n"
        "2. Pass it to the performance function with the dimensions to break down by — or pass "
        "`''` to cover every property in one query, told apart by the `site_url` column.\n"
        "3. Use the sitemap and inspection functions for crawling and indexing questions. The "
        "schema's example queries show each step.\n\n"
        "### Credentials\n\n"
        "For a local stdio worker, ATTACH with `credentials 'oauth'` to sign in through your browser "
        "to Query Farm's Google OAuth app. Each attachment carries its refreshable grant in "
        "the client's opaque attach data, so authorization survives worker restarts. Treat "
        "these bytes as credentials; do not log them. No Keychain, credential file or user "
        "app setup is needed. A new ATTACH starts a new sign-in.\n\n"
        "A `google_search_console` secret holding a service-account key (the account added to "
        "the property as a user), or an OAuth client id, client secret and refresh token also works:\n\n"
        "```sql\n"
        "CREATE SECRET gsc (TYPE google_search_console, service_account_json '<key file contents>');\n"
        "```\n\n"
        "A local worker also falls back to Application Default Credentials. Only the "
        "`webmasters.readonly` scope is ever requested.\n\n"
        "### Freshness and limits\n\n"
        "Days are Pacific Time, final data lags two or three days, and history goes back 16 "
        "months. Google returns the top rows for each day rather than every row, and inspecting "
        "URLs is limited to 2,000 a day per property."
    ),
}

_SCHEMA_TAGS = {
    "provider": "google-search-console",
    "domain": "search-analytics",
    "vgi.title": "Search Performance, Sitemaps & Indexing",
    "vgi.categories": _CATEGORIES,
    "vgi.keywords": keywords(
        "google search console", "seo", "search analytics", "queries", "sitemaps", "indexing"
    ),
    "vgi.example_queries": _EXAMPLE_QUERIES,
    "vgi.doc_llm": (
        "Read-only access to Google Search Console for every property the configured credential "
        "can read. Work in two steps: discover a property's exact site_url, then ask for search "
        "performance broken down by the dimensions you care about over a date range — for one "
        "property, or for all of them at once by passing an empty string. Results are "
        "aggregated by Google itself and come back with typed columns. Crawling and indexing are "
        "covered too: the sitemaps submitted for a property, and Google's index status for any "
        "single URL, which composes under a correlated LATERAL to inspect a list of pages."
    ),
    "vgi.doc_md": (
        "Google Search Console performance, sitemaps and index status, read-only.\n\n"
        "### Concepts\n\n"
        "- A **property** is identified by its `site_url`: `sc-domain:example.com` for a domain "
        "property, or a full URL prefix. Pass it exactly as listed, as a literal — or `''` for "
        "every property.\n"
        "- A **dimension** is an attribute to group by (date, query, page, country, device); the "
        "**metrics** — clicks, impressions, CTR and position — are always returned.\n"
        "- An **impression** is a result shown to a searcher; a **click** is one that led to the "
        "site; **position** is where the site's topmost result ranked, 1 being the top.\n\n"
        "### Everything is typed\n\n"
        "Dates are `DATE`, counts `BIGINT` and ratios `DOUBLE`, and the columns follow the "
        "dimensions requested, so a misspelled dimension fails immediately with the valid "
        "names.\n\n"
        "### The one real trap\n\n"
        "Google aggregates before SQL sees anything. Grouped by query, rare queries are withheld "
        "for privacy and are in no row; grouped by page, one search can count for several pages. "
        "So summing rows does not reproduce the property total — ask Google for the breakdown "
        "you mean, with no dimensions for a total. Likewise `ctr` and `position` are averages: "
        "recompute them from sums instead of averaging them."
    ),
}

#: Classifying tags every table carries, for faceted discovery.
_CLASSIFY = {"provider": "google-search-console", "domain": "search-analytics"}

_SITES_DOCS = docs(
    category="discovery",
    llm=(
        "Every Search Console property the credential has been added to, one row per property. "
        "This is the entry point: take `site_url` from here, exactly as written, for "
        "search_analytics(), sitemaps() and url_inspection(). An empty result means the "
        "credential has not been added to any property; a `permission_level` of "
        "siteUnverifiedUser means the property is listed but its data cannot be read."
    ),
    md=(
        "One row per property.\n\n"
        "### Two kinds of property\n\n"
        "A *domain* property (`sc-domain:example.com`) covers every protocol and subdomain of a "
        "domain. A *URL-prefix* property (`https://www.example.com/`) covers only URLs that "
        "begin with that exact prefix, so `http://` and `https://`, and `www` and the bare host, "
        "are four separate properties. A site is often registered as several, each with its own "
        "numbers — prefer the domain property when there is one.\n\n"
        "Cheap — a single API call — and cached for five minutes."
    ),
    example_queries=examples(
        (
            "Every property this credential can read",
            "SELECT site_url, property_type, permission_level FROM gsc.main.sites ORDER BY site_url",
        ),
        (
            "Properties by kind and access level",
            "SELECT property_type, permission_level, count(*) AS properties FROM gsc.main.sites "
            "GROUP BY property_type, permission_level ORDER BY properties DESC",
        ),
    ),
    extra={
        **_CLASSIFY,
        "vgi.title": "Search Console Properties",
        "vgi.keywords": keywords("sites", "properties", "discovery", "site url", "permissions"),
    },
)

_GSC_CATALOG = Catalog(
    name="gsc",
    default_schema="main",
    comment="Read-only Google Search Console: search performance, sitemaps and URL index status",
    tags=_CATALOG_TAGS,
    schemas=[
        Schema(
            path=["main"],
            comment="Search Console API (read-only, webmasters.readonly scope)",
            tags=_SCHEMA_TAGS,
            functions=list(_FUNCTIONS),
            tables=[
                Table(
                    name="sites",
                    function=AllSitesFunction,
                    comment="Every Search Console property the credential can see — the entry point",
                    tags=_SITES_DOCS,
                    column_comments=column_comments(SITE_SCHEMA),
                    primary_key=(("site_url",),),
                    not_null=("site_url",),
                ),
            ],
        ),
    ],
)


class SearchConsoleCatalog(ReadOnlyCatalogInterface):
    """Advertises the versions, the ``google_search_console`` secret type and the ATTACH options."""

    catalog = _GSC_CATALOG
    catalog_name = _GSC_CATALOG.name
    secret_types = [auth.SECRET_SPEC]
    attach_option_specs = settings.OPTION_SPECS

    def catalog_attach(self, *, name: str, options: dict[str, Any], **kwargs: Any) -> CatalogAttachResult:
        """Validate the ATTACH options and carry them to every function.

        A bad option fails the ATTACH itself rather than every later query.
        The validated settings become the catalog's attach bytes, which the
        framework hands back to each call as ``attach_opaque_data``.
        """
        validated = settings.Settings.from_options(options)
        result = super().catalog_attach(name=name, options=options, **kwargs)
        if validated.credentials == settings.OAUTH:
            validated = replace(validated, oauth_grant=oauth.authorize())
        return replace(
            result,
            attach_opaque_data=AttachOpaqueData(validated.encode()),
            attach_opaque_data_required=True,
        )

    def catalog_detach(self, *, attach_opaque_data: AttachOpaqueData) -> None:
        """Drop this worker's credential cache for the client's detached catalog."""
        attached = settings.Settings.decode(attach_opaque_data)
        if attached.credentials == settings.OAUTH:
            oauth.forget(attached.oauth_grant)

    def catalogs(self) -> list[CatalogInfo]:
        """Advertise the single read-only Search Console catalog."""
        return [
            CatalogInfo(
                name=self._effective_catalog_name,
                implementation_version=IMPLEMENTATION_VERSION,
                data_version_spec=DATA_VERSION_SPEC,
                source_url=SOURCE_URL,
                attach_option_specs=[spec.serialize() for spec in self.attach_option_specs],
            )
        ]


class SearchConsoleWorker(Worker):
    """Worker process hosting the read-only Search Console catalog."""

    catalog = _GSC_CATALOG
    catalog_interface = SearchConsoleCatalog


def main() -> None:
    """Run the worker (stdio by default; pass ``--http`` for the HTTP server)."""
    _run(http=False)


#: Operator flag for the HTTP server: permit the Application Default Credentials fallback.
ALLOW_ADC_FLAG = "--allow-adc"


def main_http() -> None:
    """Run the worker over HTTP.

    An HTTP worker may serve many clients, so it does **not** fall back to its
    own Application Default Credentials unless the operator starts it with
    ``--allow-adc`` — otherwise any client without a secret would query Search
    Console as the server. This is a command-line flag rather than an ATTACH
    option because ATTACH options are chosen by clients.
    """
    _run(http=True)


def _run(*, http: bool) -> None:
    """Restrict local credentials for every shared transport, including direct CLI flags."""
    argv = sys.argv[1:]
    shared = http or any(arg.split("=", maxsplit=1)[0] in {"--http", "--tcp", "--unix"} for arg in argv)
    oauth.permit_local(not shared)
    auth.permit_adc(not shared or ALLOW_ADC_FLAG in argv)
    argv = [arg for arg in argv if arg != ALLOW_ADC_FLAG]
    if http and "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    SearchConsoleWorker.main()
