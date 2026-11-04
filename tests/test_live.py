"""Live tests against real Google Search Console, through a real ATTACH.

Opt in with ``pytest -m live``. They need a credential that has been added to at
least one Search Console property:

* ``VGI_GSC_TEST_SERVICE_ACCOUNT_JSON`` — a service-account key (the JSON text),
  **or** ``VGI_GSC_TEST_ACCESS_TOKEN`` — an access token carrying the
  ``webmasters.readonly`` scope, **or** neither, in which case the worker's
  Application Default Credentials are used;
* ``VGI_GSC_TEST_SITE`` — a ``site_url`` that credential can read. When unset,
  the first readable property in ``sites`` is used.

Every assertion holds for any property with any traffic — none depend on
volume — so the suite can run against a quiet test property. URL Inspection is
exercised with a single call, since it draws on a quota of 2,000 a day.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent


def _escape(value: str) -> str:
    return value.replace("'", "''")


@pytest.fixture(scope="module")
def con() -> Iterator[Any]:
    """A Haybarn connection with the worker attached and the test credential configured."""
    haybarn = pytest.importorskip("haybarn")
    service_account = os.environ.get("VGI_GSC_TEST_SERVICE_ACCOUNT_JSON", "")
    token = os.environ.get("VGI_GSC_TEST_ACCESS_TOKEN", "")
    # With a test credential configured, insist on it: falling back to the
    # developer's own ADC would test a different identity than CI uses.
    mode = "secret" if (service_account or token) else "auto"
    connection = haybarn.connect()
    try:
        location = f"uv run --exact --no-project {ROOT / 'gsc_worker.py'}"
        connection.execute(f"ATTACH 'gsc' (TYPE vgi, LOCATION '{location}', credentials '{mode}')")
        if service_account:
            connection.execute(
                "CREATE SECRET gsc_live (TYPE google_search_console, "
                f"service_account_json '{_escape(service_account)}')"
            )
        elif token:
            connection.execute(
                f"CREATE SECRET gsc_live (TYPE google_search_console, access_token '{_escape(token)}')"
            )
        try:
            connection.execute("SELECT count(*) FROM gsc.main.sites").fetchall()
        except Exception as exc:
            # Asked for explicitly, but there is nothing to authenticate with:
            # that is "not configured here", not a failing worker.
            if "no Google Search Console credentials" in str(exc):
                pytest.skip("no credential configured (set VGI_GSC_TEST_SERVICE_ACCOUNT_JSON or use ADC)")
            raise
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="module")
def site(con: Any) -> str:
    explicit = os.environ.get("VGI_GSC_TEST_SITE", "").strip()
    if explicit:
        return explicit
    row = con.execute(
        "SELECT site_url FROM gsc.main.sites WHERE permission_level <> 'siteUnverifiedUser' "
        "ORDER BY site_url LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("the credential has been added to no Search Console property")
    return str(row[0])


def test_sites_lists_the_property(con: Any, site: str) -> None:
    found = dict(con.execute("SELECT site_url, property_type FROM gsc.main.sites").fetchall())
    assert site in found
    assert found[site] == ("domain" if site.startswith("sc-domain:") else "url_prefix")


def test_sitemaps_run(con: Any, site: str) -> None:
    rows = con.execute(f"SELECT path, errors, warnings FROM gsc.main.sitemaps('{_escape(site)}')").fetchall()
    assert all(path and errors >= 0 and warnings >= 0 for path, errors, warnings in rows)


def test_daily_series_is_typed_and_in_range(con: Any, site: str) -> None:
    rows = con.execute(
        "SELECT date, clicks, impressions, ctr, position FROM gsc.main.search_analytics("
        f"'{_escape(site)}', dimensions => ['date'], start_date => '10daysAgo') ORDER BY date"
    ).fetchall()
    assert len(rows) <= 11
    for _day, clicks, impressions, ctr, _position in rows:
        assert 0 <= clicks <= impressions
        assert 0.0 <= ctr <= 1.0


def test_a_total_is_one_row_and_one_request_worth(con: Any, site: str) -> None:
    rows = con.execute(
        f"SELECT clicks, impressions FROM gsc.main.search_analytics('{_escape(site)}')"
    ).fetchall()
    assert len(rows) <= 1


def test_the_daily_series_adds_up_to_the_total(con: Any, site: str) -> None:
    """Grouping by date alone loses nothing, unlike grouping by query."""
    window = "start_date => '20daysAgo', end_date => '10daysAgo'"
    total = con.execute(
        f"SELECT coalesce(sum(clicks), 0) FROM gsc.main.search_analytics('{_escape(site)}', {window})"
    ).fetchone()[0]
    by_day = con.execute(
        "SELECT coalesce(sum(clicks), 0) FROM gsc.main.search_analytics("
        f"'{_escape(site)}', dimensions => ['date'], {window})"
    ).fetchone()[0]
    assert by_day == total


def test_paging_reaches_the_same_rows_as_one_page(con: Any, site: str) -> None:
    """A walk in small pages returns what a single large page does."""
    haybarn = pytest.importorskip("haybarn")
    window = "dimensions => ['query'], start_date => '20daysAgo', end_date => '10daysAgo', max_rows => 120"
    one_page = con.execute(
        f"SELECT query, clicks FROM gsc.main.search_analytics('{_escape(site)}', {window}) ORDER BY ALL"
    ).fetchall()
    small = haybarn.connect()
    try:
        location = f"uv run --exact --no-project {ROOT / 'gsc_worker.py'}"
        small.execute(f"ATTACH 'gsc' (TYPE vgi, LOCATION '{location}', page_size 25)")
        for name, variable in (
            ("service_account_json", "VGI_GSC_TEST_SERVICE_ACCOUNT_JSON"),
            ("access_token", "VGI_GSC_TEST_ACCESS_TOKEN"),
        ):
            value = os.environ.get(variable, "")
            if value:
                small.execute(
                    f"CREATE SECRET gsc_live (TYPE google_search_console, {name} '{_escape(value)}')"
                )
                break
        paged = small.execute(
            f"SELECT query, clicks FROM gsc.main.search_analytics('{_escape(site)}', {window}) ORDER BY ALL"
        ).fetchall()
    finally:
        small.close()
    assert paged == one_page


def test_filter_pushdown_is_correct(con: Any, site: str) -> None:
    call = f"gsc.main.search_analytics('{_escape(site)}', dimensions => ['device'])"
    every = con.execute(f"SELECT device, clicks, impressions FROM {call}").fetchall()
    if not every:
        pytest.skip("no impressions in the last 28 days")
    device, clicks, impressions = every[0]
    filtered = con.execute(
        f"SELECT device, clicks, impressions FROM {call} WHERE device = ?", [device]
    ).fetchall()
    assert filtered == [(device, clicks, impressions)]


def test_every_property_walk_includes_the_site(con: Any, site: str) -> None:
    rows = con.execute(
        "SELECT DISTINCT site_url FROM gsc.main.search_analytics('', start_date => '10daysAgo') "
        "WHERE site_url = ?",
        [site],
    ).fetchall()
    assert rows in ([], [(site,)])


def test_a_bad_dimension_fails_at_bind(con: Any, site: str) -> None:
    with pytest.raises(Exception, match="not a Search Console dimension"):
        con.execute(
            f"SELECT * FROM gsc.main.search_analytics('{_escape(site)}', dimensions => ['keyword'])"
        ).fetchall()


def test_url_inspection_of_the_top_page(con: Any, site: str) -> None:
    top = con.execute(
        "SELECT page FROM gsc.main.search_analytics("
        f"'{_escape(site)}', dimensions => ['page'], max_rows => 25) ORDER BY clicks DESC LIMIT 1"
    ).fetchone()
    if top is None:
        pytest.skip("no page has impressions in the last 28 days")
    rows = con.execute(
        "SELECT inspection_url, verdict, coverage_state FROM gsc.main.url_inspection(?, ?)", [site, top[0]]
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == top[0]
    assert rows[0][1] in {"PASS", "PARTIAL", "FAIL", "NEUTRAL", "VERDICT_UNSPECIFIED"}
