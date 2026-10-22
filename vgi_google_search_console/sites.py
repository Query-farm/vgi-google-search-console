"""Properties and their sitemaps: where the key every other function needs comes from.

``sites`` lists every Search Console property the credential has been added to
and takes no argument, so it is the way in; the ``site_url`` it returns is the
first argument of everything else.

``sitemaps()`` also accepts ``''`` for "every property this credential can
read", which turns a sitemap audit (which sites have a sitemap with errors?
which have none at all?) into one query rather than one per site.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from vgi_google_search_console import gsc_api
from vgi_google_search_console.common import LISTING_TTL_SECONDS, REQUIRED_SECRETS, emit, session
from vgi_google_search_console.gsc_api import Session
from vgi_google_search_console.meta import docs, examples
from vgi_google_search_console.pushdown import build_filtered, discrete_strings
from vgi_google_search_console.schemas import SITE_SCHEMA, SITEMAP_SCHEMA

_log = logging.getLogger(__name__)

#: A property the credential is listed on but may not read.
UNVERIFIED = "siteUnverifiedUser"


# ---------------------------------------------------------------------------
# Flatteners: one API resource → one row
# ---------------------------------------------------------------------------


def flatten_site(entry: dict[str, Any]) -> dict[str, Any]:
    """A ``sites`` resource as a row."""
    site = entry.get("siteUrl")
    return {
        "site_url": site,
        "permission_level": entry.get("permissionLevel"),
        "property_type": gsc_api.property_type(site),
    }


def flatten_sitemap(site: str, sitemap: dict[str, Any]) -> dict[str, Any]:
    """A ``sitemaps`` resource as a row, stamped with the site it belongs to."""
    contents = [
        {"type": item.get("type"), "submitted": item.get("submitted"), "indexed": item.get("indexed")}
        for item in sitemap.get("contents") or []
        if isinstance(item, dict)
    ]
    return {
        "site_url": site,
        "path": sitemap.get("path"),
        "type": sitemap.get("type"),
        "is_sitemaps_index": sitemap.get("isSitemapsIndex", False),
        "is_pending": sitemap.get("isPending", False),
        "last_submitted": sitemap.get("lastSubmitted"),
        "last_downloaded": sitemap.get("lastDownloaded"),
        "warnings": sitemap.get("warnings"),
        "errors": sitemap.get("errors"),
        "contents": contents,
    }


def readable_sites(sess: Session) -> list[str]:
    """Every site URL the credential can actually read, in a stable order."""
    return sorted(
        str(entry["siteUrl"])
        for entry in gsc_api.sites(sess)
        if entry.get("siteUrl") and entry.get("permissionLevel") != UNVERIFIED
    )


def site_targets(params: ProcessParams[Any], sess: Session, named: str) -> list[str]:
    """The sites a per-site scan should visit, in order.

    A site named in the argument is the only target. With ``''`` the scan walks
    every readable site — narrowed to the ones a ``WHERE site_url = ...`` or
    ``IN (...)`` names, when there is one, so that a filter on the column costs
    what naming the site would have.
    """
    if named:
        return [gsc_api.site_url(named)]
    readable = readable_sites(sess)
    wanted = discrete_strings(params, "site_url")
    if wanted is None:
        return readable
    return [site for site in readable if site in wanted]


# ---------------------------------------------------------------------------
# sites
# ---------------------------------------------------------------------------


@init_single_worker
class AllSitesFunction(TableFunctionGenerator[None, None]):
    """Every property the credential can see — the scan behind the ``sites`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SITE_SCHEMA

    class Meta:
        name = "all_sites"
        description = "Every Search Console property the credential can see (the scan backing `sites`)"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=SITE_SCHEMA,
            llm=(
                "Every Search Console property the configured credential has been added to, one "
                "row per property. Prefer the `sites` table, which scans this. It is the starting "
                "point for everything else: the `site_url` column is the first argument of "
                "search_analytics(), sitemaps() and url_inspection(), and must be passed exactly "
                "as it appears here."
            ),
            md=(
                "One row per property.\n\n"
                "### Prefer the table\n\n"
                "The `sites` table is backed by this function and returns exactly the same rows; "
                "this name exists only so the function and the table can share a schema.\n\n"
                "### An empty result\n\n"
                "means the credential works but has been added to no property. Add its email (for "
                "a service account, the `client_email` in its key) as a user under Settings → "
                "Users and permissions in Search Console."
            ),
            example_queries=examples(
                (
                    "Every property and what the credential may do with it",
                    "SELECT site_url, property_type, permission_level "
                    "FROM gsc.main.all_sites() ORDER BY site_url",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT site_url, property_type, permission_level "
                    "FROM gsc.main.all_sites() ORDER BY site_url"
                ),
                description="Every property and what the credential may do with it",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        """Declare the fixed schema."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch the whole list — Google does not page it — and emit it."""
        rows = [flatten_site(entry) for entry in gsc_api.sites(session(params))]
        batch, _ = build_filtered(params, rows)
        emit(out, batch, LISTING_TTL_SECONDS)
        out.finish()


# ---------------------------------------------------------------------------
# sitemaps()
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class SitemapsArgs:
    """``sitemaps(site_url)`` — one property, or ``''`` for every readable property."""

    site_url: Annotated[
        str,
        Arg(
            0,
            doc="Property, e.g. 'sc-domain:example.com' or 'https://www.example.com/', exactly as "
            "the sites table lists it. Empty string = every property the credential can read.",
        ),
    ]
    sitemap_index: Annotated[
        str,
        Arg(
            "sitemap_index",
            doc="URL of a sitemap index; lists the sitemaps inside it instead of those submitted "
            "directly. Needs a site_url.",
            default="",
        ),
    ] = ""


@dataclass(kw_only=True)
class SiteWalkState(ArrowSerializableDataclass):
    """Which sites a per-site scan still has to visit, persisted between ticks.

    ``targets_json`` is the JSON list of sites still to walk, head first. It is
    a string because scan state must round-trip through Arrow.
    """

    targets_json: str = ""
    started: bool = False
    done: bool = False


@init_single_worker
class SitemapsFunction(TableFunctionGenerator[SitemapsArgs, SiteWalkState]):
    """The sitemaps submitted for a property, one site per ``process()`` tick."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SITEMAP_SCHEMA

    class Meta:
        name = "sitemaps"
        description = "Sitemaps submitted for a Search Console property, with their status and contents"
        categories = ["sitemaps"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="sitemaps",
            result_schema=SITEMAP_SCHEMA,
            llm=(
                "The sitemaps submitted to Search Console for a property: when each was last "
                "submitted and fetched, whether Google is still processing it, how many errors "
                "and warnings it has, and how many URLs it lists by content type. Use it to audit "
                "sitemap health. Takes a site_url from the `sites` table, or '' to walk every "
                "property the credential can read."
            ),
            md=(
                "One row per sitemap.\n\n"
                "### Contents\n\n"
                "`contents` is a list with one entry per content type the sitemap lists (`web`, "
                "`image`, `video`, `news`, …) and the number of URLs submitted for it. Apply "
                "`unnest()` to get one row per type. Google has deprecated the `indexed` count and "
                "no longer fills it in; use url_inspection() to ask whether a specific URL is "
                "indexed.\n\n"
                "### Sitemap indexes\n\n"
                "A sitemap index (`is_sitemaps_index`) lists other sitemaps rather than pages. "
                "Pass its `path` as `sitemap_index` to list the sitemaps inside it.\n\n"
                "### Every property at once\n\n"
                "Pass `''` instead of a site_url to walk every property the credential can read — "
                "the `site_url` column says which each row came from, and a `WHERE site_url = ...` "
                "visits only that property. A property that refuses the listing is skipped."
            ),
            example_queries=examples(
                (
                    "Sitemaps with errors or warnings, across every property",
                    "SELECT site_url, path, errors, warnings, last_downloaded "
                    "FROM gsc.main.sitemaps('') WHERE errors > 0 OR warnings > 0 "
                    "ORDER BY errors DESC, warnings DESC",
                ),
                (
                    "URLs submitted per content type, for each sitemap",
                    "SELECT site_url, path, c.type AS content_type, c.submitted "
                    "FROM (SELECT site_url, path, unnest(contents) AS c FROM gsc.main.sitemaps('')) "
                    "ORDER BY site_url, path, content_type",
                ),
                (
                    "Properties with no sitemap submitted at all",
                    "SELECT s.site_url FROM gsc.main.sites s "
                    "WHERE s.permission_level <> 'siteUnverifiedUser' AND s.site_url NOT IN ("
                    "SELECT site_url FROM gsc.main.sitemaps('')) ORDER BY s.site_url",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT site_url, path, errors, warnings, last_downloaded "
                    "FROM gsc.main.sitemaps('') WHERE errors > 0 OR warnings > 0 "
                    "ORDER BY errors DESC, warnings DESC"
                ),
                description="Sitemaps with errors or warnings, across every property",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[SitemapsArgs]) -> BindResponse:
        """Validate the site up front, then declare the fixed schema."""
        args = params.args
        if args.site_url.strip():
            gsc_api.site_url(args.site_url)
        elif args.sitemap_index.strip():
            raise ValueError("sitemap_index names a sitemap index of one property; pass that site_url too")
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[SitemapsArgs]) -> SiteWalkState:
        """Start before the first site."""
        return SiteWalkState()

    @classmethod
    def process(cls, params: ProcessParams[SitemapsArgs], state: SiteWalkState, out: OutputCollector) -> None:
        """Emit one site's sitemaps, then move to the next site."""
        if state.done:
            out.finish()
            return
        args = params.args
        sess = session(params)
        named = args.site_url.strip()
        if not state.started:
            state.targets_json = json.dumps(site_targets(params, sess, named))
            state.started = True
        targets: list[str] = json.loads(state.targets_json)
        if not targets:
            state.done = True
            out.finish()
            return
        site = targets.pop(0)
        try:
            found = gsc_api.sitemaps(site, sess, sitemap_index=args.sitemap_index.strip())
        except gsc_api.SearchConsoleError as exc:
            # Only a refusal of this one site is stepped over, and only when
            # the caller did not name it; see `is_access_refusal`.
            if named or not exc.is_access_refusal:
                raise
            _log.warning("skipping %s during an every-site walk: %s", site, exc)
            found = []
        state.targets_json = json.dumps(targets)
        state.done = not targets
        batch, _ = build_filtered(params, [flatten_sitemap(site, item) for item in found])
        emit(out, batch, LISTING_TTL_SECONDS)


SITE_FUNCTIONS: list[type] = [AllSitesFunction, SitemapsFunction]
