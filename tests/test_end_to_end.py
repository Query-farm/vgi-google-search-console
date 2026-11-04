"""SQL executed against a real ATTACH, offline.

The worker runs as a subprocess launched by the vgi DuckDB extension (shipped in
Haybarn), exactly as a user runs it, and talks over a real socket to
:mod:`tests.fake_gsc`. That exercises what unit tests cannot: argument binding,
bind-time schemas, secret delivery, filter pushdown, projection, paging across
``process()`` ticks and LATERAL provenance — while every request the worker
makes is visible in ``fake_gsc.REQUESTS`` for assertions.

The live counterpart, against real Search Console, is ``test_live.py``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import fake_gsc
from vgi_google_search_console import worker

ROOT = Path(__file__).resolve().parent.parent

#: Small pages so a modest synthetic result spans several ticks.
PAGE_SIZE = 10

SITE = fake_gsc.DOMAIN_SITE
OTHER = fake_gsc.PREFIX_SITE
#: The whole synthetic fact table, so results do not depend on the clock.
RANGE = "start_date => '2026-09-01', end_date => '2026-09-28'"

TOTAL_CLICKS = sum(f["clicks"] for f in fake_gsc.FACTS)
TOTAL_IMPRESSIONS = sum(f["impressions"] for f in fake_gsc.FACTS)


def attach_sql(url: str, **extra: Any) -> str:
    """An ATTACH aimed at the fake through ATTACH options — no environment variables."""
    options = {"api_url": url, "page_size": PAGE_SIZE, **extra}
    rendered = ", ".join(f"{k} {v!r}" if isinstance(v, str) else f"{k} {v}" for k, v in options.items())
    location = f"uv run --exact --no-project {ROOT / 'gsc_worker.py'}"
    return f"ATTACH 'gsc' (TYPE vgi, LOCATION '{location}', {rendered})"


@pytest.fixture(scope="module")
def fake_url() -> Iterator[str]:
    with fake_gsc.serve() as url:
        yield url


@pytest.fixture(scope="module")
def con(fake_url: str) -> Iterator[Any]:
    """A Haybarn connection with the worker attached to the fake and a secret set."""
    haybarn = pytest.importorskip("haybarn")
    connection = haybarn.connect()
    try:
        connection.execute(attach_sql(fake_url))
        connection.execute(
            f"CREATE SECRET my_gsc (TYPE google_search_console, access_token '{fake_gsc.TOKEN}')"
        )
        yield connection
    finally:
        connection.close()


def rows(con: Any, sql: str) -> list[tuple[Any, ...]]:
    return list(con.execute(sql).fetchall())


def queries() -> list[tuple[str, dict[str, Any]]]:
    """``(site, body)`` for every searchAnalytics/query request the worker made."""
    prefix, suffix = "/webmasters/v3/sites/", "/searchAnalytics/query"
    return [
        (path.removeprefix(prefix).removesuffix(suffix), body or {})
        for _m, path, _q, body in fake_gsc.REQUESTS
        if path.endswith(suffix)
    ]


def bodies() -> list[dict[str, Any]]:
    return [body for _site, body in queries()]


def filters_sent(body: dict[str, Any]) -> list[dict[str, str]]:
    return [f for group in body.get("dimensionFilterGroups", []) for f in group["filters"]]


def analytics(arguments: str, site: str = SITE) -> str:
    return f"gsc.main.search_analytics('{site}', {arguments})"


@pytest.fixture(autouse=True)
def _clear_log() -> None:
    fake_gsc.REQUESTS.clear()


class TestSites:
    def test_table_constraints_are_visible_to_sql(self, con: Any) -> None:
        assert rows(
            con,
            "SELECT constraint_type, constraint_column_names FROM duckdb_constraints() "
            "WHERE database_name = 'gsc' AND table_name = 'sites' ORDER BY constraint_type",
        ) == [("NOT NULL", ["site_url"]), ("PRIMARY KEY", ["site_url"])]

    def test_site_url_is_non_nullable(self, con: Any) -> None:
        assert rows(
            con,
            "SELECT column_name FROM duckdb_columns() "
            "WHERE database_name = 'gsc' AND table_name = 'sites' AND NOT is_nullable",
        ) == [("site_url",)]

    def test_sites_table(self, con: Any) -> None:
        got = rows(con, "SELECT site_url, permission_level, property_type FROM gsc.main.sites ORDER BY 1")
        assert got == [
            ("https://locked.example/", "siteRestrictedUser", "url_prefix"),
            ("https://unverified.example/", "siteUnverifiedUser", "url_prefix"),
            ("https://www.example.com/", "siteFullUser", "url_prefix"),
            ("sc-domain:example.com", "siteOwner", "domain"),
        ]

    def test_table_and_scan_function_agree(self, con: Any) -> None:
        assert rows(
            con,
            "SELECT (SELECT count(*) FROM gsc.main.sites) = (SELECT count(*) FROM gsc.main.all_sites())",
        ) == [(True,)]

    def test_a_predicate_on_the_table_is_applied(self, con: Any) -> None:
        assert rows(con, "SELECT site_url FROM gsc.main.sites WHERE property_type = 'domain'") == [(SITE,)]
        assert rows(con, "SELECT count(*) FROM gsc.main.sites WHERE site_url LIKE 'ftp%'") == [(0,)]


class TestSitemaps:
    def test_one_site(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT path, is_sitemaps_index, is_pending, warnings, errors, typeof(last_submitted) "
            f"FROM gsc.main.sitemaps('{SITE}') ORDER BY path",
        )
        assert got == [
            ("https://www.example.com/broken.xml", False, True, 2, 3, "TIMESTAMP WITH TIME ZONE"),
            ("https://www.example.com/sitemap.xml", True, False, 0, 0, "TIMESTAMP WITH TIME ZONE"),
        ]

    def test_a_sitemap_never_downloaded_has_a_null_time(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT last_downloaded IS NULL FROM gsc.main.sitemaps('{SITE}') WHERE path LIKE '%broken%'",
        )
        assert got == [(True,)]

    def test_contents_unnest_to_one_row_per_type(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT c.type, sum(c.submitted) FROM (SELECT unnest(contents) AS c "
            f"FROM gsc.main.sitemaps('{SITE}')) GROUP BY 1 ORDER BY 1",
        )
        assert got == [("image", 7), ("web", 160)]

    def test_every_site_skips_the_ones_that_refuse(self, con: Any) -> None:
        got = rows(con, "SELECT site_url, count(*) FROM gsc.main.sitemaps('') GROUP BY 1")
        assert got == [(SITE, 2)]
        walked = [p for _m, p, _q, _b in fake_gsc.REQUESTS if p.endswith("/sitemaps")]
        # Every readable-looking site is tried; the unverified one is not.
        assert len(walked) == 3
        assert not any("unverified" in p for p in walked)

    def test_a_predicate_on_site_url_visits_only_that_site(self, con: Any) -> None:
        got = rows(con, f"SELECT count(*) FROM gsc.main.sitemaps('') WHERE site_url = '{SITE}'")
        assert got == [(2,)]
        walked = [p for _m, p, _q, _b in fake_gsc.REQUESTS if p.endswith("/sitemaps")]
        assert walked == [f"/webmasters/v3/sites/{SITE}/sitemaps"]

    def test_a_named_site_that_refuses_is_an_error(self, con: Any) -> None:
        with pytest.raises(Exception, match="Users and permissions"):
            rows(con, f"SELECT * FROM gsc.main.sitemaps('{fake_gsc.LOCKED_SITE}')")

    def test_sitemap_index(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT path FROM gsc.main.sitemaps('{SITE}', "
            "sitemap_index => 'https://www.example.com/sitemap.xml')",
        )
        assert got == [("https://www.example.com/sitemap-pages.xml",)]

    def test_sitemap_index_needs_a_site(self, con: Any) -> None:
        with pytest.raises(Exception, match="pass that site_url too"):
            rows(con, "SELECT * FROM gsc.main.sitemaps('', sitemap_index => 'https://x.example/s.xml')")

    def test_sites_without_a_sitemap(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT s.site_url FROM gsc.main.sites s WHERE s.permission_level <> 'siteUnverifiedUser' "
            "AND s.site_url NOT IN (SELECT site_url FROM gsc.main.sitemaps('')) ORDER BY 1",
        )
        assert got == [("https://locked.example/",), ("https://www.example.com/",)]


class TestSearchAnalytics:
    def test_columns_are_typed(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT typeof(site_url), typeof(date), typeof(query), typeof(clicks), typeof(impressions), "
            f"typeof(ctr), typeof(position) FROM {analytics(f"dimensions => ['date', 'query'], {RANGE}")} "
            "LIMIT 1",
        )
        assert got == [("VARCHAR", "DATE", "VARCHAR", "BIGINT", "BIGINT", "DOUBLE", "DOUBLE")]

    def test_no_dimensions_is_one_total_row(self, con: Any) -> None:
        got = rows(con, f"SELECT site_url, clicks, impressions FROM {analytics(RANGE)}")
        assert got == [(SITE, TOTAL_CLICKS, TOTAL_IMPRESSIONS)]
        # One row at most, so there is no second page to probe for.
        assert len(queries()) == 1

    def test_daily_totals(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*), min(date), max(date), sum(clicks) "
            f"FROM {analytics(f"dimensions => ['date'], {RANGE}")}",
        )
        assert got == [(28, fake_gsc.FACT_DATES[0], fake_gsc.FACT_DATES[-1], TOTAL_CLICKS)]

    def test_columns_follow_the_order_requested(self, con: Any) -> None:
        call = analytics("dimensions => ['device', 'country']")
        described = rows(con, f"SELECT column_name FROM (DESCRIBE SELECT * FROM {call})")
        assert [name for (name,) in described] == [
            "site_url",
            "device",
            "country",
            "clicks",
            "impressions",
            "ctr",
            "position",
        ]

    def test_pages_are_walked_in_order_until_an_empty_one(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*), count(DISTINCT (query, page, country)) "
            f"FROM {analytics(f"dimensions => ['query', 'page', 'country'], {RANGE}")}",
        )
        assert got == [(36, 36)]
        assert [(b["startRow"], b["rowLimit"]) for b in bodies()] == [
            (0, PAGE_SIZE),
            (10, PAGE_SIZE),
            (20, PAGE_SIZE),
            (30, PAGE_SIZE),
            (36, PAGE_SIZE),
        ]

    def test_a_limit_stops_the_walk_early(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT * FROM {analytics(f"dimensions => ['query', 'page', 'country'], {RANGE}")} LIMIT 3",
        )
        assert len(got) == 3
        assert len(queries()) <= 2

    def test_max_rows(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*) "
            f"FROM {analytics(f"dimensions => ['query', 'page', 'country'], {RANGE}, max_rows => 15")}",
        )
        assert got == [(15,)]
        assert [b["rowLimit"] for b in bodies()] == [10, 5]

    def test_rows_arrive_sorted_by_clicks(self, con: Any) -> None:
        got = rows(con, f"SELECT clicks FROM {analytics(f"dimensions => ['query'], {RANGE}")}")
        assert [c for (c,) in got] == sorted((c for (c,) in got), reverse=True)

    def test_the_hour_dimension_is_an_instant(self, con: Any) -> None:
        arguments = (
            "dimensions => ['hour'], start_date => '2026-09-01', end_date => '2026-09-01', "
            "data_state => 'hourly_all'"
        )
        [(kind, first)] = rows(con, f"SELECT typeof(hour), min(hour) FROM {analytics(arguments)} GROUP BY 1")
        assert kind == "TIMESTAMP WITH TIME ZONE"
        # Midnight Pacific Daylight Time on the first is 07:00 UTC.
        assert first.astimezone(dt.UTC) == dt.datetime(2026, 9, 1, 7, tzinfo=dt.UTC)

    @pytest.mark.parametrize("spelling", ["search_appearance", "searchAppearance"])
    def test_search_appearance_and_its_camel_case_spelling(self, con: Any, spelling: str) -> None:
        got = rows(
            con,
            "SELECT search_appearance, clicks "
            f"FROM {analytics(f"dimensions => ['{spelling}'], {RANGE}")} ORDER BY clicks DESC",
        )
        assert got == [("VIDEO", 30), ("AMP_BLUE_LINK", 12)]
        assert bodies()[0]["dimensions"] == ["searchAppearance"]

    def test_search_type_reaches_google_in_its_own_spelling(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT DISTINCT query "
            f"FROM {analytics(f"dimensions => ['query'], {RANGE}, search_type => 'image'")}",
        )
        assert got == [(fake_gsc.QUERIES[0],)]
        assert bodies()[0]["type"] == "image"
        rows(con, f"SELECT * FROM {analytics(f"{RANGE}, search_type => 'google_news'")}")
        assert bodies()[-1]["type"] == "googleNews"

    def test_discover_has_no_position(self, con: Any) -> None:
        got = rows(con, f"SELECT position FROM {analytics(f"{RANGE}, search_type => 'discover'")}")
        assert got == [(None,)]

    def test_aggregation_type_and_data_state_are_sent(self, con: Any) -> None:
        rows(
            con,
            f"SELECT * FROM {analytics(f"{RANGE}, aggregation_type => 'by_page', data_state => 'all'")}",
        )
        assert bodies()[0]["aggregationType"] == "byPage"
        assert bodies()[0]["dataState"] == "all"

    def test_relative_dates_are_resolved_to_iso_dates(self, con: Any) -> None:
        rows(con, f"SELECT * FROM {analytics("start_date => '7daysAgo'")}")
        body = bodies()[0]
        start, end = dt.date.fromisoformat(body["startDate"]), dt.date.fromisoformat(body["endDate"])
        assert (end - start).days == 7

    def test_the_filters_argument_filters_on_a_hidden_dimension(self, con: Any) -> None:
        filters = json.dumps(
            [
                {"dimension": "country", "operator": "equals", "expression": "usa"},
                {"dimension": "query", "operator": "not_contains", "expression": "arrow"},
            ]
        )
        call = analytics(f"dimensions => ['device'], {RANGE}, filters => '{filters}'")
        got = rows(con, f"SELECT sum(clicks) FROM {call}")
        expected = sum(
            f["clicks"] for f in fake_gsc.FACTS if f["country"] == "usa" and "arrow" not in f["query"]
        )
        assert got == [(expected,)]
        assert filters_sent(bodies()[0]) == [
            {"dimension": "country", "operator": "equals", "expression": "usa"},
            {"dimension": "query", "operator": "notContains", "expression": "arrow"},
        ]

    @pytest.mark.parametrize(
        ("call", "message"),
        [
            (analytics("dimensions => ['querry']"), "not a Search Console dimension"),
            (analytics("dimensions => ['clicks']"), "is a metric"),
            (analytics("dimensions => ['date', 'date']"), "requested twice"),
            ("gsc.main.search_analytics('example.com')", "sc-domain:example.com"),
            (analytics("start_date => 'last week'"), "does not match the required pattern"),
            (analytics("start_date => '2026-09-10', end_date => '2026-09-01'"), "is after end_date"),
            (analytics("dimensions => ['hour']"), "hourly_all"),
            (analytics("filters => '{'"), "not valid JSON"),
            (
                analytics('filters => \'[{"dimension": "date", "expression": "x"}]\''),
                "cannot filter on 'date'",
            ),
            (analytics("search_type => 'maps'"), "maps"),
        ],
    )
    def test_mistakes_fail_at_bind_with_a_reason(self, con: Any, call: str, message: str) -> None:
        with pytest.raises(Exception, match=message):
            rows(con, f"SELECT * FROM {call}")
        assert queries() == []

    def test_the_site_cannot_be_a_subquery(self, con: Any) -> None:
        """Why '' exists: the engine refuses anything but a literal here."""
        with pytest.raises(Exception, match="subquer"):
            rows(
                con,
                "SELECT * FROM gsc.main.search_analytics((SELECT site_url FROM gsc.main.sites LIMIT 1))",
            )

    def test_googles_own_refusals_come_through(self, con: Any) -> None:
        with pytest.raises(Exception, match="cannot be grouped with other dimensions"):
            rows(con, f"SELECT * FROM {analytics("dimensions => ['search_appearance', 'query']")}")

    def test_a_named_site_the_credential_cannot_read_is_an_error(self, con: Any) -> None:
        with pytest.raises(Exception, match="Users and permissions"):
            rows(con, f"SELECT * FROM {analytics(RANGE, site=fake_gsc.LOCKED_SITE)}")


class TestEveryProperty:
    """``search_analytics('')`` walks every readable property."""

    def test_each_readable_site_contributes_its_rows(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT site_url, clicks FROM gsc.main.search_analytics('', {RANGE}) ORDER BY site_url",
        )
        assert got == [(OTHER, TOTAL_CLICKS), (SITE, TOTAL_CLICKS)]
        # The locked site is tried and skipped; the unverified one is never asked.
        assert [site for site, _body in queries()] == [fake_gsc.LOCKED_SITE, OTHER, SITE]

    def test_each_site_is_paged_from_its_own_first_row(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT site_url, count(*) FROM gsc.main.search_analytics("
            f"'', dimensions => ['query', 'page'], {RANGE}) GROUP BY 1 ORDER BY 1",
        )
        assert got == [(OTHER, 12), (SITE, 12)]
        offsets = [(site, body["startRow"]) for site, body in queries() if site != fake_gsc.LOCKED_SITE]
        assert offsets == [(OTHER, 0), (OTHER, 10), (OTHER, 12), (SITE, 0), (SITE, 10), (SITE, 12)]

    def test_max_rows_applies_to_each_site(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT site_url, count(*) FROM gsc.main.search_analytics("
            f"'', dimensions => ['query', 'page'], {RANGE}, max_rows => 4) GROUP BY 1 ORDER BY 1",
        )
        assert got == [(OTHER, 4), (SITE, 4)]

    def test_a_predicate_on_site_url_visits_only_that_site(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT count(*) FROM gsc.main.search_analytics('', dimensions => ['date'], {RANGE}) "
            f"WHERE site_url = '{SITE}'",
        )
        assert got == [(28,)]
        assert {site for site, _body in queries()} == {SITE}

    def test_an_in_list_visits_only_those_sites(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT DISTINCT site_url FROM gsc.main.search_analytics('', {RANGE}) "
            f"WHERE site_url IN ('{SITE}', 'sc-domain:nowhere.example')",
        )
        assert got == [(SITE,)]
        assert [site for site, _body in queries()] == [SITE]

    def test_an_unpushable_predicate_on_site_url_is_still_applied(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT DISTINCT site_url FROM gsc.main.search_analytics('', {RANGE}) "
            "WHERE site_url LIKE 'sc-domain:%'",
        )
        assert got == [(SITE,)]

    @pytest.mark.parametrize(
        "sql",
        [f"SELECT * FROM gsc.main.search_analytics('', {RANGE})", "SELECT * FROM gsc.main.sitemaps('')"],
        ids=["search_analytics", "sitemaps"],
    )
    def test_an_exhausted_quota_fails_the_walk_instead_of_emptying_it(self, con: Any, sql: str) -> None:
        """Both are 403s. Only "you may not read this site" may be stepped over."""
        fake_gsc.THROTTLED.add(OTHER)
        try:
            with pytest.raises(Exception, match="quota is exhausted"):
                rows(con, sql)
        finally:
            fake_gsc.THROTTLED.clear()

    def test_the_documented_join_shape(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT s.permission_level, a.clicks FROM gsc.main.sites s JOIN "
            f"gsc.main.search_analytics('', {RANGE}) a USING (site_url) ORDER BY 1",
        )
        assert got == [("siteFullUser", TOTAL_CLICKS), ("siteOwner", TOTAL_CLICKS)]


class TestFiltersAreApplied:
    """With filter_pushdown declared, the engine drops its own filter — so the worker must apply it."""

    BASE = f"FROM {analytics(f"dimensions => ['country', 'device', 'page'], {RANGE}")}"

    def test_equality_is_pushed_to_google(self, con: Any) -> None:
        got = rows(con, f"SELECT DISTINCT country {self.BASE} WHERE country = 'deu'")
        assert got == [("deu",)]
        assert filters_sent(bodies()[0]) == [
            {"dimension": "country", "operator": "equals", "expression": "deu"}
        ]

    def test_several_equalities_are_all_pushed(self, con: Any) -> None:
        got = rows(
            con, f"SELECT count(*) {self.BASE} WHERE device = 'MOBILE' AND page = '{fake_gsc.PAGES[1]}'"
        )
        assert got == [(3,)]
        assert sorted(f["dimension"] for f in filters_sent(bodies()[0])) == ["device", "page"]

    def test_a_value_google_would_not_recognise_is_not_pushed(self, con: Any) -> None:
        """'USA' can match no row; sending it risks an API error where the answer is simply empty."""
        assert rows(con, f"SELECT count(*) {self.BASE} WHERE country = 'USA'") == [(0,)]
        assert filters_sent(bodies()[0]) == []

    def test_in_is_applied_by_the_worker(self, con: Any) -> None:
        got = rows(con, f"SELECT DISTINCT country {self.BASE} WHERE country IN ('deu', 'gbr') ORDER BY 1")
        assert got == [("deu",), ("gbr",)]
        assert filters_sent(bodies()[0]) == []

    def test_unpushable_predicates_are_still_applied(self, con: Any) -> None:
        got = rows(con, f"SELECT DISTINCT page {self.BASE} WHERE page LIKE '%/docs' ORDER BY 1")
        assert got == [(fake_gsc.PAGES[1],)]

    @pytest.mark.parametrize(
        "predicate",
        ["country = 'zzz'", "clicks > 999999999", "page LIKE 'ftp%'", "device <> device", "ctr > 1"],
    )
    def test_an_impossible_predicate_returns_nothing(self, con: Any, predicate: str) -> None:
        assert rows(con, f"SELECT count(*) {self.BASE} WHERE {predicate}") == [(0,)]

    def test_a_metric_predicate_selects_matching_rows(self, con: Any) -> None:
        got = rows(con, f"SELECT clicks {self.BASE} WHERE clicks > 150")
        assert got
        assert all(value > 150 for (value,) in got)

    def test_a_filter_on_an_unselected_column_is_applied(self, con: Any) -> None:
        got = rows(con, f"SELECT sum(clicks) {self.BASE} WHERE device = 'TABLET'")
        assert got == [(sum(f["clicks"] for f in fake_gsc.FACTS if f["device"] == "TABLET"),)]


class TestDatePushdown:
    BASE = f"FROM {analytics(f"dimensions => ['date'], {RANGE}")}"

    def sent_range(self) -> tuple[str, str]:
        return bodies()[0]["startDate"], bodies()[0]["endDate"]

    def test_a_range_on_date_narrows_the_request(self, con: Any) -> None:
        got = rows(
            con, f"SELECT count(*) {self.BASE} WHERE date >= DATE '2026-09-10' AND date <= DATE '2026-09-12'"
        )
        assert got == [(3,)]
        assert self.sent_range() == ("2026-09-10", "2026-09-12")

    def test_strict_bounds_are_fetched_inclusively_and_filtered(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT min(date), max(date) {self.BASE} "
            "WHERE date > DATE '2026-09-10' AND date < DATE '2026-09-13'",
        )
        assert got == [(dt.date(2026, 9, 11), dt.date(2026, 9, 12))]
        assert self.sent_range() == ("2026-09-10", "2026-09-13")

    def test_the_predicate_can_only_narrow_the_arguments(self, con: Any) -> None:
        got = rows(con, f"SELECT count(*) {self.BASE} WHERE date >= DATE '2020-01-01'")
        assert got == [(28,)]
        assert self.sent_range() == ("2026-09-01", "2026-09-28")

    def test_an_equality_fetches_one_day(self, con: Any) -> None:
        got = rows(con, f"SELECT date {self.BASE} WHERE date = DATE '2026-09-05'")
        assert got == [(dt.date(2026, 9, 5),)]
        assert self.sent_range() == ("2026-09-05", "2026-09-05")

    def test_a_range_outside_the_arguments_asks_google_nothing(self, con: Any) -> None:
        assert rows(con, f"SELECT count(*) {self.BASE} WHERE date > DATE '2027-01-01'") == [(0,)]
        assert fake_gsc.REQUESTS == []


class TestUrlInspection:
    def test_a_literal_call(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT site_url, inspection_url, verdict, coverage_state, crawled_as, sitemaps, "
            "rich_results_verdict, rich_result_types "
            f"FROM gsc.main.url_inspection('{SITE}', '{fake_gsc.PAGES[0]}')",
        )
        assert got == [
            (
                SITE,
                fake_gsc.PAGES[0],
                "PASS",
                "Submitted and indexed",
                "MOBILE",
                ["https://www.example.com/sitemap.xml"],
                "PARTIAL",
                ["Breadcrumbs", "FAQ"],
            )
        ]

    def test_nested_issues(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT i.rich_result_type, i.item_name, i.severity FROM (SELECT unnest(rich_result_issues) AS i "
            f"FROM gsc.main.url_inspection('{SITE}', '{fake_gsc.PAGES[0]}'))",
        )
        assert got == [("FAQ", "Pricing FAQ", "WARNING")]
        got = rows(
            con,
            "SELECT amp_verdict, amp_issues[1].severity, last_crawl_time "
            f"FROM gsc.main.url_inspection('{SITE}', '{fake_gsc.PAGES[2]}')",
        )
        assert got == [("FAIL", "ERROR", None)]

    def test_search_analytics_feeds_it_both_columns(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT p.page, i.inspection_url, i.verdict "
            f"FROM (SELECT site_url, page FROM {analytics(f"dimensions => ['page'], {RANGE}")}) p, "
            "LATERAL gsc.main.url_inspection(p.site_url, p.page) i ORDER BY p.page",
        )
        assert got == [
            (fake_gsc.PAGES[0], fake_gsc.PAGES[0], "PASS"),
            (fake_gsc.PAGES[1], fake_gsc.PAGES[1], "NEUTRAL"),
            (fake_gsc.PAGES[2], fake_gsc.PAGES[2], "FAIL"),
        ]

    def test_a_literal_site_with_a_column_url(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT t.u FROM (VALUES ('{fake_gsc.PAGES[0]}'), ('{fake_gsc.PAGES[1]}')) t(u), "
            f"LATERAL gsc.main.url_inspection('{SITE}', t.u) i "
            "WHERE i.google_canonical IS DISTINCT FROM i.user_canonical",
        )
        assert got == [(fake_gsc.PAGES[1],)]

    def test_rows_from_different_sites_stay_with_their_inputs(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT t.s, t.u, i.verdict FROM (VALUES ('{SITE}', '{fake_gsc.PAGES[1]}'), "
            f"('{OTHER}', '{fake_gsc.PAGES[0]}')) t(s, u), "
            "LATERAL gsc.main.url_inspection(t.s, t.u) i ORDER BY 2",
        )
        assert got == [(OTHER, fake_gsc.PAGES[0], "PASS"), (SITE, fake_gsc.PAGES[1], "NEUTRAL")]

    def test_a_null_url_produces_no_row_and_no_request(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*) FROM (VALUES (NULL::VARCHAR)) t(u), "
            f"LATERAL gsc.main.url_inspection('{SITE}', t.u) i",
        )
        assert got == [(0,)]
        assert not any(p.endswith(":inspect") for _m, p, _q, _b in fake_gsc.REQUESTS)

    def test_language_code_is_sent(self, con: Any) -> None:
        rows(
            con,
            f"SELECT verdict FROM gsc.main.url_inspection('{SITE}', '{fake_gsc.PAGES[0]}', "
            "language_code => 'de-DE')",
        )
        [body] = [b for _m, p, _q, b in fake_gsc.REQUESTS if p.endswith(":inspect")]
        assert body == {"inspectionUrl": fake_gsc.PAGES[0], "siteUrl": SITE, "languageCode": "de-DE"}

    @pytest.mark.parametrize(
        ("site", "url", "message"),
        [
            (SITE, "https://elsewhere.example/", "not part of this property"),
            (SITE, "/pricing", "not a URL to inspect"),
            ("example.com", fake_gsc.PAGES[0], "sc-domain:example.com"),
        ],
    )
    def test_mistakes_are_errors_not_empty_rows(self, con: Any, site: str, url: str, message: str) -> None:
        with pytest.raises(Exception, match=message):
            rows(con, f"SELECT * FROM gsc.main.url_inspection('{site}', '{url}')")


class TestCredentials:
    def test_no_secret_is_a_clear_error(self, con: Any) -> None:
        con.execute("DROP SECRET my_gsc")
        try:
            with pytest.raises(Exception, match="CREATE SECRET"):
                rows(con, "SELECT * FROM gsc.main.sites")
        finally:
            con.execute(f"CREATE SECRET my_gsc (TYPE google_search_console, access_token '{fake_gsc.TOKEN}')")

    def test_a_wrong_token_is_rejected_with_context(self, con: Any) -> None:
        con.execute("CREATE OR REPLACE SECRET my_gsc (TYPE google_search_console, access_token 'wrong')")
        try:
            with pytest.raises(Exception, match="401"):
                rows(con, f"SELECT * FROM gsc.main.sitemaps('{SITE}')")
        finally:
            con.execute(
                "CREATE OR REPLACE SECRET my_gsc "
                f"(TYPE google_search_console, access_token '{fake_gsc.TOKEN}')"
            )

    def test_secret_values_are_redacted(self, con: Any) -> None:
        [(secret,)] = rows(con, "SELECT secret_string FROM duckdb_secrets() WHERE name = 'my_gsc'")
        assert fake_gsc.TOKEN not in secret


class TestAttachOptions:
    @pytest.mark.parametrize(
        ("option", "message"),
        [
            ("credentials 'required'", "not one of auto, secret"),
            ("page_size 0", "between 1 and 25000"),
            ("api_url 'file:///etc/passwd'", "http"),
        ],
    )
    def test_a_bad_option_fails_the_attach(self, option: str, message: str) -> None:
        haybarn = pytest.importorskip("haybarn")
        connection = haybarn.connect()
        location = f"uv run --exact --no-project {ROOT / 'gsc_worker.py'}"
        try:
            with pytest.raises(Exception, match=message):
                connection.execute(f"ATTACH 'gsc' (TYPE vgi, LOCATION '{location}', {option})")
        finally:
            connection.close()

    def test_page_size_option_controls_paging(self, con: Any) -> None:
        rows(con, f"SELECT count(*) FROM {analytics(f"dimensions => ['query', 'page'], {RANGE}")}")
        assert {b["rowLimit"] for b in bodies()} == {PAGE_SIZE}


def _documented_examples() -> list[tuple[str, str]]:
    schema = worker._GSC_CATALOG.schemas[0]
    blobs = [("schema", worker._SCHEMA_TAGS["vgi.example_queries"])]
    blobs += [(fn.Meta.name, fn.Meta.tags["vgi.example_queries"]) for fn in worker._FUNCTIONS]
    blobs += [(table.name, table.tags["vgi.example_queries"]) for table in schema.tables]
    return [
        (f"{owner}: {example['description']}", example["sql"])
        for owner, blob in blobs
        for example in json.loads(blob)
    ]


def _grader_queries() -> list[tuple[str, str]]:
    graders = yaml.safe_load((ROOT / "vgi-agent-tests.yaml").read_text())["tasks"]
    found = []
    for grader in graders:
        reference = grader["reference_sql"]
        for sql in [reference] if isinstance(reference, str) else reference:
            found.append((grader["name"], sql))
        if grader.get("check_sql"):
            found.append((f"{grader['name']} (check)", grader["check_sql"]))
    return found


DOCUMENTED = _documented_examples()
GRADERS = _grader_queries()


class TestCatalogExamples:
    """What the catalog tells an agent to run has to run — here, not only in a live tier."""

    @pytest.mark.parametrize(
        "example", json.loads(worker._EXECUTABLE_EXAMPLES), ids=lambda example: example["name"]
    )
    def test_executable_examples_hold(self, con: Any, example: dict[str, Any]) -> None:
        assert [list(row) for row in rows(con, example["sql"])] == example["expected_result"]

    @pytest.mark.parametrize(
        "sql", [sql for _label, sql in DOCUMENTED], ids=[label for label, _sql in DOCUMENTED]
    )
    def test_documented_examples_run(self, con: Any, sql: str) -> None:
        rows(con, sql)

    @pytest.mark.parametrize("sql", [sql for _name, sql in GRADERS], ids=[name for name, _sql in GRADERS])
    def test_agent_reference_queries_run(self, con: Any, sql: str) -> None:
        rows(con, sql)
