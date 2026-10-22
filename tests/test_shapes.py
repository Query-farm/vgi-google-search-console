"""Pure functions: value coercion, API-resource flattening, and request building."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa
import pytest

from tests import fake_gsc
from vgi_google_search_console import schemas
from vgi_google_search_console.inspection import flatten_inspection
from vgi_google_search_console.meta import comment_of, result_columns_schema
from vgi_google_search_console.pushdown import date_bounds, dimension_filters, discrete_strings
from vgi_google_search_console.search_analytics import (
    SearchAnalyticsArgs,
    analytics_rows,
    build_request,
    normalise_dimensions,
    parse_filters,
    resolve_date,
    search_analytics_schema,
)
from vgi_google_search_console.sites import flatten_site, flatten_sitemap

TODAY = dt.date(2026, 9, 29)


class TestCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(12, 12), ("34", 34), (56.0, 56), ("7.0", 7), (1.5, None), ("x", None), (None, None), (True, None)],
    )
    def test_to_int(self, raw: Any, expected: int | None) -> None:
        assert schemas.to_int(raw) == expected

    def test_to_int_rejects_what_int64_cannot_hold(self) -> None:
        assert schemas.to_int(2**63) is None
        assert schemas.to_int(float("inf")) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.25, 0.25),
            ("0.5", 0.5),
            (3, 3.0),
            ("nan", None),
            (float("inf"), None),
            ("", None),
            (False, None),
        ],
    )
    def test_to_float(self, raw: Any, expected: float | None) -> None:
        assert schemas.to_float(raw) == expected

    def test_to_date(self) -> None:
        assert schemas.to_date("2026-09-05") == dt.date(2026, 9, 5)
        assert schemas.to_date("20260905") == dt.date(2026, 9, 5)  # Python accepts the basic form too
        assert schemas.to_date("yesterday") is None
        assert schemas.to_date(None) is None

    def test_to_timestamp_converts_an_offset_to_utc(self) -> None:
        assert schemas.to_timestamp("2026-09-01T00:00:00-07:00") == dt.datetime(2026, 9, 1, 7, tzinfo=dt.UTC)
        assert schemas.to_timestamp("2026-09-01T08:00:00.000Z") == dt.datetime(2026, 9, 1, 8, tzinfo=dt.UTC)

    @pytest.mark.parametrize("raw", ["", "soon", None, 5, "0001-01-01T00:00:00Z"])
    def test_to_timestamp_is_total(self, raw: Any) -> None:
        """Year 1 parses but overflows nanosecond clients, so it is NULL rather than an error later."""
        assert schemas.to_timestamp(raw) is None

    def test_one_bad_value_does_not_fail_the_batch(self) -> None:
        schema = pa.schema([("n", pa.int64()), ("t", schemas.TIMESTAMP), ("d", pa.date32())])
        batch = schemas.batch_from_rows(
            [{"n": "1", "t": "2026-09-01T00:00:00Z", "d": "2026-09-01"}, {"n": "x", "t": "x", "d": "x"}],
            schema,
        )
        assert batch.to_pylist()[1] == {"n": None, "t": None, "d": None}

    def test_nested_values_are_coerced_field_by_field(self) -> None:
        kind = pa.list_(schemas.SITEMAP_CONTENT)
        [got] = schemas.column([{"c": [{"type": "web", "submitted": "9", "extra": 1}, "junk"]}], "c", kind)
        assert got.as_py() == [{"type": "web", "submitted": 9, "indexed": None}, None]


class TestFlatteners:
    def test_site(self) -> None:
        assert flatten_site({"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}) == {
            "site_url": "sc-domain:example.com",
            "permission_level": "siteOwner",
            "property_type": "domain",
        }

    def test_sitemap_counts_arrive_as_strings(self) -> None:
        row = flatten_sitemap("s", fake_gsc.SITEMAPS[fake_gsc.DOMAIN_SITE][1])
        batch = schemas.batch_from_rows([row], schemas.SITEMAP_SCHEMA)
        [got] = batch.to_pylist()
        assert (got["site_url"], got["warnings"], got["errors"], got["is_pending"]) == ("s", 2, 3, True)
        assert got["last_downloaded"] is None
        assert [c["submitted"] for c in got["contents"]] == [40, 7]

    def test_a_bare_sitemap_still_makes_a_row(self) -> None:
        [got] = schemas.batch_from_rows(
            [flatten_sitemap("s", {"path": "p"})], schemas.SITEMAP_SCHEMA
        ).to_pylist()
        assert got["contents"] == []
        assert got["is_sitemaps_index"] is False

    def test_inspection_merges_the_three_sections(self) -> None:
        row = flatten_inspection("s", "u", fake_gsc.INSPECTIONS[fake_gsc.PAGES[0]])
        [got] = schemas.batch_from_rows([row], schemas.URL_INSPECTION_SCHEMA).to_pylist()
        assert got["verdict"] == "PASS"
        assert got["last_crawl_time"] == dt.datetime(2026, 9, 26, 10, 11, 12, tzinfo=dt.UTC)
        assert got["rich_result_types"] == ["Breadcrumbs", "FAQ"]
        assert got["rich_result_issues"] == [
            {
                "rich_result_type": "FAQ",
                "item_name": "Pricing FAQ",
                "severity": "WARNING",
                "issue_message": "Missing field 'name'",
            }
        ]
        assert got["amp_verdict"] is None
        assert got["amp_issues"] == []

    def test_an_empty_inspection_is_a_row_of_nulls(self) -> None:
        [got] = schemas.batch_from_rows(
            [flatten_inspection("s", "u", {})], schemas.URL_INSPECTION_SCHEMA
        ).to_pylist()
        assert (got["site_url"], got["inspection_url"], got["verdict"]) == ("s", "u", None)
        assert got["sitemaps"] == []

    def test_every_fixed_schema_documents_every_column(self) -> None:
        for schema in (schemas.SITE_SCHEMA, schemas.SITEMAP_SCHEMA, schemas.URL_INSPECTION_SCHEMA):
            assert all(comment_of(f) for f in schema)
            result_columns_schema(schema)  # every type has a SQL rendering


def _args(**overrides: Any) -> SearchAnalyticsArgs:
    return SearchAnalyticsArgs(site_url="sc-domain:example.com", **overrides)


class TestDates:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("today", TODAY),
            ("yesterday", dt.date(2026, 9, 28)),
            ("28daysAgo", dt.date(2026, 9, 1)),
            ("0daysAgo", TODAY),
            ("2026-01-31", dt.date(2026, 1, 31)),
            (" today ", TODAY),
        ],
    )
    def test_resolve(self, expr: str, expected: dt.date) -> None:
        assert resolve_date("start_date", expr, TODAY) == expected

    @pytest.mark.parametrize("expr", ["", "last week", "2026-13-01", "2026-02-30", "20260101", "-3daysAgo"])
    def test_anything_else_names_the_accepted_forms(self, expr: str) -> None:
        with pytest.raises(ValueError, match="NdaysAgo"):
            resolve_date("start_date", expr, TODAY)


class TestDimensions:
    def test_both_spellings_map_to_the_column_name(self) -> None:
        assert normalise_dimensions(["Date", "searchAppearance"]) == ["date", "search_appearance"]
        assert normalise_dimensions(["SEARCH_APPEARANCE"]) == ["search_appearance"]

    def test_schema_is_site_then_dimensions_then_metrics(self) -> None:
        schema = search_analytics_schema(["date", "hour", "query"])
        assert schema.names == [
            "site_url",
            "date",
            "hour",
            "query",
            "clicks",
            "impressions",
            "ctr",
            "position",
        ]
        assert schema.field("date").type == pa.date32()
        assert schema.field("hour").type == schemas.TIMESTAMP
        assert schema.field("clicks").type == pa.int64()
        assert all(comment_of(f) for f in schema)


class TestFiltersArgument:
    def test_a_single_object_and_an_array_are_both_accepted(self) -> None:
        one = '{"dimension": "page", "operator": "contains", "expression": "/docs"}'
        assert parse_filters(one) == parse_filters(f"[{one}]")
        assert parse_filters(one) == [{"dimension": "page", "operator": "contains", "expression": "/docs"}]

    def test_the_operator_defaults_to_equals(self) -> None:
        assert parse_filters('[{"dimension": "device", "expression": "MOBILE"}]')[0]["operator"] == "equals"

    @pytest.mark.parametrize(
        ("spelling", "api"),
        [
            ("not_equals", "notEquals"),
            ("notContains", "notContains"),
            ("INCLUDING_REGEX", "includingRegex"),
            ("excludingRegex", "excludingRegex"),
        ],
    )
    def test_operators_in_either_spelling(self, spelling: str, api: str) -> None:
        raw = f'[{{"dimension": "query", "operator": "{spelling}", "expression": "x"}}]'
        assert parse_filters(raw)[0]["operator"] == api

    def test_dimensions_are_sent_in_the_api_spelling(self) -> None:
        raw = '[{"dimension": "search_appearance", "expression": "VIDEO"}]'
        assert parse_filters(raw)[0]["dimension"] == "searchAppearance"

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("{", "not valid JSON"),
            ('"country"', "JSON array of objects"),
            ("[1]", "JSON array of objects"),
            ('[{"dimension": "hour", "expression": "x"}]', "cannot filter on 'hour'"),
            ('[{"dimension": "page", "operator": "like", "expression": "x"}]', "unknown operator 'like'"),
            ('[{"dimension": "page"}]', "non-empty string 'expression'"),
            ('[{"dimension": "page", "expression": 5}]', "non-empty string 'expression'"),
        ],
    )
    def test_mistakes_explain_themselves(self, raw: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_filters(raw)

    def test_empty_is_no_filter(self) -> None:
        assert parse_filters("  ") == []


class TestBuildRequest:
    def test_defaults(self) -> None:
        assert build_request(_args(), [], TODAY) == {
            "startDate": "2026-09-01",
            "endDate": "2026-09-29",
            "dimensions": [],
            "type": "web",
            "aggregationType": "auto",
            "dataState": "final",
        }

    def test_everything_is_translated_to_the_api_spelling(self) -> None:
        body = build_request(
            _args(
                dimensions=["search_appearance"],
                search_type="google_news",
                aggregation_type="by_news_showcase_panel",
                data_state="all",
            ),
            [],
            TODAY,
        )
        assert body is not None
        assert (body["dimensions"], body["type"], body["aggregationType"], body["dataState"]) == (
            ["searchAppearance"],
            "googleNews",
            "byNewsShowcasePanel",
            "all",
        )

    def test_caller_and_pushed_filters_are_one_and_group(self) -> None:
        pushed = [{"dimension": "device", "operator": "equals", "expression": "MOBILE"}]
        body = build_request(_args(filters='[{"dimension": "country", "expression": "usa"}]'), pushed, TODAY)
        assert body is not None
        [group] = body["dimensionFilterGroups"]
        assert group["groupType"] == "and"
        assert [f["dimension"] for f in group["filters"]] == ["country", "device"]

    def test_where_bounds_only_narrow(self) -> None:
        args = _args(start_date="2026-09-10", end_date="2026-09-20")
        wide = build_request(args, [], TODAY, first=dt.date(2026, 1, 1), last=dt.date(2026, 12, 31))
        narrow = build_request(args, [], TODAY, first=dt.date(2026, 9, 12), last=dt.date(2026, 9, 14))
        assert wide is not None
        assert narrow is not None
        assert (wide["startDate"], wide["endDate"]) == ("2026-09-10", "2026-09-20")
        assert (narrow["startDate"], narrow["endDate"]) == ("2026-09-12", "2026-09-14")

    def test_disjoint_bounds_mean_there_is_nothing_to_ask(self) -> None:
        assert build_request(_args(), [], TODAY, first=dt.date(2027, 1, 1)) is None

    def test_reversed_arguments_are_a_mistake_not_an_empty_result(self) -> None:
        with pytest.raises(ValueError, match="is after end_date"):
            build_request(_args(start_date="today", end_date="yesterday"), [], TODAY)

    def test_hour_needs_hourly_data(self) -> None:
        with pytest.raises(ValueError, match="hourly_all"):
            build_request(_args(dimensions=["hour"]), [], TODAY)
        assert build_request(_args(dimensions=["hour"], data_state="hourly_all"), [], TODAY) is not None


class TestRows:
    def test_keys_map_to_columns_by_position(self) -> None:
        response = {
            "rows": [{"keys": ["2026-09-01", "duckdb"], "clicks": 3.0, "impressions": 40.0, "ctr": 0.075}]
        }
        [row] = analytics_rows(response, ["date", "query"], "sc-domain:example.com")
        batch = schemas.batch_from_rows([row], search_analytics_schema(["date", "query"]))
        assert batch.to_pylist() == [
            {
                "site_url": "sc-domain:example.com",
                "date": dt.date(2026, 9, 1),
                "query": "duckdb",
                "clicks": 3,
                "impressions": 40,
                "ctr": 0.075,
                "position": None,
            }
        ]

    def test_no_rows_key_is_no_rows(self) -> None:
        assert analytics_rows({"responseAggregationType": "byProperty"}, ["date"], "s") == []


class _Scalar:
    def __init__(self, value: Any) -> None:
        self.value = value

    def as_py(self) -> Any:
        return self.value


class _Bounds:
    def __init__(self, low: Any, high: Any) -> None:
        self.min_value = None if low is None else _Scalar(low)
        self.max_value = None if high is None else _Scalar(high)


class _Filters:
    """The three accessors the pushdown helpers read, with canned answers."""

    def __init__(
        self,
        constants: dict[str, Any] | None = None,
        values: dict[str, list[Any]] | None = None,
        bounds: dict[str, _Bounds] | None = None,
    ) -> None:
        self.constants, self.values, self.bounds = constants or {}, values or {}, bounds or {}

    def get_column_constant(self, column: str) -> _Scalar | None:
        return _Scalar(self.constants[column]) if column in self.constants else None

    def get_column_values(self, column: str) -> pa.Array | None:
        return pa.array(self.values[column]) if column in self.values else None

    def get_column_bounds(self, column: str) -> _Bounds | None:
        return self.bounds.get(column)


class _Params:
    def __init__(self, filters: _Filters | None) -> None:
        self.current_pushdown_filters = filters


def _params(**kwargs: Any) -> Any:
    return _Params(_Filters(**kwargs))


class TestPushdownTranslation:
    """What reaches Google must never be narrower than the predicate it came from."""

    def test_no_filters_pushes_nothing(self) -> None:
        params: Any = _Params(None)
        assert dimension_filters(params, ["page"]) == []
        assert date_bounds(params) == (None, None)
        assert discrete_strings(params, "site_url") is None

    def test_equality_on_the_four_pushable_dimensions(self) -> None:
        params = _params(
            constants={"page": "https://a/", "query": "duckdb", "country": "usa", "device": "MOBILE"}
        )
        got = dimension_filters(params, ["page", "query", "country", "device"])
        assert [(f["dimension"], f["operator"], f["expression"]) for f in got] == [
            ("page", "equals", "https://a/"),
            ("query", "equals", "duckdb"),
            ("country", "equals", "usa"),
            ("device", "equals", "MOBILE"),
        ]

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("country", "USA"),
            ("country", "us"),
            ("device", "mobile"),
            ("device", "PHONE"),
            ("search_appearance", "VIDEO"),
            ("date", "2026-09-01"),
            ("page", ""),
            ("page", 5),
        ],
    )
    def test_anything_uncertain_is_left_for_the_worker(self, column: str, value: Any) -> None:
        assert dimension_filters(_params(constants={column: value}), [column]) == []

    def test_only_requested_columns_are_considered(self) -> None:
        assert dimension_filters(_params(constants={"page": "https://a/"}), ["query"]) == []

    def test_date_bounds_are_returned_inclusive_and_unadjusted(self) -> None:
        params = _params(bounds={"date": _Bounds(dt.date(2026, 9, 2), dt.datetime(2026, 9, 9, 12))})
        assert date_bounds(params) == (dt.date(2026, 9, 2), dt.date(2026, 9, 9))
        assert date_bounds(_params(bounds={"date": _Bounds(None, dt.date(2026, 9, 9))})) == (
            None,
            dt.date(2026, 9, 9),
        )

    def test_a_bound_that_is_not_a_date_is_ignored(self) -> None:
        assert date_bounds(_params(bounds={"date": _Bounds("2026-09-02", 5)})) == (None, None)

    def test_discrete_strings_dedupes_and_refuses_mixed_types(self) -> None:
        assert discrete_strings(_params(values={"site_url": ["a", "b", "a"]}), "site_url") == ["a", "b"]
        assert discrete_strings(_params(values={"site_url": [1, 2]}), "site_url") is None
        assert discrete_strings(_params(values={"other": ["a"]}), "site_url") is None
