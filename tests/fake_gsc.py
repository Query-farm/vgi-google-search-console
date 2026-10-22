"""A small, deterministic stand-in for the Search Console API.

It speaks just enough of the REST API for the worker's real request and
response code to run unchanged against it — in-process through
``httpx.MockTransport`` (:func:`handle`) or out-of-process over a real socket
(:func:`serve`), which is how the end-to-end tests drive a worker subprocess
attached from DuckDB.

The data is synthetic but internally consistent: a fact table of
(date, query, page, country, device) rows with integer clicks and impressions,
which ``searchAnalytics/query`` groups by the requested dimensions and sums,
sorted by clicks as Google does.

Every request is recorded in :data:`REQUESTS`, so tests can assert what the
worker actually asked Google for — the pushed-down filter, the page offsets.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

TOKEN = "test-token"

DOMAIN_SITE = "sc-domain:example.com"
PREFIX_SITE = "https://www.example.com/"
LOCKED_SITE = "https://locked.example/"
UNVERIFIED_SITE = "https://unverified.example/"

SITES = [
    {"siteUrl": DOMAIN_SITE, "permissionLevel": "siteOwner"},
    {"siteUrl": PREFIX_SITE, "permissionLevel": "siteFullUser"},
    {"siteUrl": LOCKED_SITE, "permissionLevel": "siteRestrictedUser"},
    {"siteUrl": UNVERIFIED_SITE, "permissionLevel": "siteUnverifiedUser"},
]

#: Sites whose data can be read. The locked one is listed with a readable
#: permission level but refuses every per-site call, like a property whose
#: access was revoked after the list was cached.
READABLE = {DOMAIN_SITE, PREFIX_SITE}

#: Sites that currently answer every per-site call with an exhausted quota.
#: Tests add to it and must clear it again.
THROTTLED: set[str] = set()

#: Every request seen, as (method, decoded path, query, body).
REQUESTS: list[tuple[str, str, dict[str, list[str]], dict[str, Any] | None]] = []
_lock = threading.Lock()

FACT_DATES = [date(2026, 9, 1) + timedelta(days=i) for i in range(28)]
QUERIES = ["duckdb", "duckdb extension", "vgi worker", "arrow flight"]
PAGES = ["https://www.example.com/", "https://www.example.com/docs", "https://www.example.com/pricing"]
COUNTRIES = ["usa", "gbr", "deu"]
DEVICES = ["DESKTOP", "MOBILE", "TABLET"]


def _facts() -> list[dict[str, Any]]:
    rows = []
    for i, (day, query, page, country, device) in enumerate(
        product(FACT_DATES, QUERIES, PAGES, COUNTRIES, DEVICES)
    ):
        clicks = (i * 7) % 11
        rows.append(
            {
                "date": day.isoformat(),
                "hour": f"{day.isoformat()}T{i % 24:02d}:00:00-07:00",
                "query": query,
                "page": page,
                "country": country,
                "device": device,
                "clicks": clicks,
                "impressions": clicks * 10 + 5,
                "position": 1 + (i % 9),
            }
        )
    return rows


FACTS = _facts()

SITEMAPS = {
    DOMAIN_SITE: [
        {
            "path": "https://www.example.com/sitemap.xml",
            "lastSubmitted": "2026-09-01T08:00:00.000Z",
            "lastDownloaded": "2026-09-27T03:04:05.000Z",
            "isPending": False,
            "isSitemapsIndex": True,
            "type": "sitemap",
            "warnings": "0",
            "errors": "0",
            "contents": [{"type": "web", "submitted": "120", "indexed": "0"}],
        },
        {
            "path": "https://www.example.com/broken.xml",
            "lastSubmitted": "2026-08-01T08:00:00.000Z",
            "isPending": True,
            "isSitemapsIndex": False,
            "type": "sitemap",
            "warnings": "2",
            "errors": "3",
            "contents": [
                {"type": "web", "submitted": "40", "indexed": "0"},
                {"type": "image", "submitted": "7", "indexed": "0"},
            ],
        },
    ],
    PREFIX_SITE: [],
}

#: The sitemaps listed inside the domain property's sitemap index.
INDEX_CHILDREN = [
    {
        "path": "https://www.example.com/sitemap-pages.xml",
        "lastSubmitted": "2026-09-01T08:00:00.000Z",
        "isPending": False,
        "isSitemapsIndex": False,
        "type": "sitemap",
        "warnings": "0",
        "errors": "0",
        "contents": [{"type": "web", "submitted": "120", "indexed": "0"}],
    }
]

INSPECTIONS: dict[str, dict[str, Any]] = {
    PAGES[0]: {
        "inspectionResultLink": "https://search.google.com/search-console/inspect?id=home",
        "indexStatusResult": {
            "verdict": "PASS",
            "coverageState": "Submitted and indexed",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "lastCrawlTime": "2026-09-26T10:11:12Z",
            "pageFetchState": "SUCCESSFUL",
            "googleCanonical": PAGES[0],
            "userCanonical": PAGES[0],
            "sitemap": ["https://www.example.com/sitemap.xml"],
            "referringUrls": ["https://www.example.com/docs"],
            "crawledAs": "MOBILE",
        },
        "richResultsResult": {
            "verdict": "PARTIAL",
            "detectedItems": [
                {
                    "richResultType": "Breadcrumbs",
                    "items": [{"name": "Home"}],
                },
                {
                    "richResultType": "FAQ",
                    "items": [
                        {
                            "name": "Pricing FAQ",
                            "issues": [{"issueMessage": "Missing field 'name'", "severity": "WARNING"}],
                        }
                    ],
                },
            ],
        },
    },
    PAGES[1]: {
        "inspectionResultLink": "https://search.google.com/search-console/inspect?id=docs",
        "indexStatusResult": {
            "verdict": "NEUTRAL",
            "coverageState": "Alternate page with proper canonical tag",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "lastCrawlTime": "2026-09-20T00:00:00Z",
            "pageFetchState": "SUCCESSFUL",
            "googleCanonical": "https://www.example.com/documentation",
            "userCanonical": PAGES[1],
            "crawledAs": "DESKTOP",
        },
    },
    PAGES[2]: {
        "indexStatusResult": {
            "verdict": "FAIL",
            "coverageState": "Excluded by 'noindex' tag",
            "robotsTxtState": "ALLOWED",
            "indexingState": "BLOCKED_BY_META_TAG",
            "pageFetchState": "SUCCESSFUL",
        },
        "ampResult": {
            "verdict": "FAIL",
            "ampUrl": "https://www.example.com/pricing/amp",
            "issues": [{"issueMessage": "Invalid attribute", "severity": "ERROR"}],
        },
    },
}


def _error(status: int, reason: str, message: str) -> tuple[int, dict[str, Any]]:
    """An error in the older dialect the webmasters half of the API answers in."""
    return status, {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "global", "reason": reason}],
        }
    }


def _forbidden(site: str) -> tuple[int, dict[str, Any]]:
    return _error(403, "forbidden", f"User does not have sufficient permission for site '{site}'.")


_OPERATORS = {
    "equals": lambda value, expr: value == expr,
    "notEquals": lambda value, expr: value != expr,
    "contains": lambda value, expr: expr in value,
    "notContains": lambda value, expr: expr not in value,
    "includingRegex": lambda value, expr: re.search(expr, value) is not None,
    "excludingRegex": lambda value, expr: re.search(expr, value) is None,
}


def _matches(fact: dict[str, Any], groups: list[dict[str, Any]]) -> bool:
    for group in groups:
        for flt in group.get("filters", []):
            value = str(fact.get(flt["dimension"], ""))
            if not _OPERATORS[flt.get("operator", "equals")](value, flt["expression"]):
                return False
    return True


def search_analytics(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Group, sum, sort by clicks and page — the fake ``searchAnalytics/query``."""
    limit = body.get("rowLimit", 1000)
    offset = body.get("startRow", 0)
    if not 1 <= limit <= 25_000:
        return _error(400, "invalid", "rowLimit must be between 1 and 25000")
    start, end = body.get("startDate", ""), body.get("endDate", "")
    if start > end:
        return _error(400, "invalid", "startDate must not be after endDate")
    dims: list[str] = body.get("dimensions", [])
    if "searchAppearance" in dims:
        if dims != ["searchAppearance"]:
            return _error(400, "invalid", "searchAppearance cannot be grouped with other dimensions")
        rows = [
            {"keys": ["VIDEO"], "clicks": 30.0, "impressions": 400.0, "ctr": 0.075, "position": 4.5},
            {"keys": ["AMP_BLUE_LINK"], "clicks": 12.0, "impressions": 90.0, "ctr": 0.1333, "position": 7.0},
        ]
        return 200, {"rows": rows[offset : offset + limit]}
    if "hour" in dims and body.get("dataState") != "hourly_all":
        return _error(400, "invalid", "hour requires dataState HOURLY_ALL")

    kind = body.get("type", "web")
    groups: dict[tuple[str, ...], list[float]] = {}
    for fact in FACTS:
        if not start <= fact["date"] <= end:
            continue
        # Image search only knows the first query, so a search_type is visible in results.
        if kind == "image" and fact["query"] != QUERIES[0]:
            continue
        if not _matches(fact, body.get("dimensionFilterGroups", [])):
            continue
        key = tuple(str(fact[d]) for d in dims)
        acc = groups.setdefault(key, [0.0, 0.0, 0.0])
        acc[0] += fact["clicks"]
        acc[1] += fact["impressions"]
        acc[2] += fact["position"] * fact["impressions"]

    ordered = sorted(groups.items(), key=lambda item: (-item[1][0], item[0]))
    rows = []
    for key, (clicks, impressions, weighted) in ordered[offset : offset + limit]:
        row: dict[str, Any] = {
            "keys": list(key),
            "clicks": clicks,
            "impressions": impressions,
            "ctr": clicks / impressions if impressions else 0.0,
        }
        if kind != "discover":  # Discover has no ranking
            row["position"] = weighted / impressions if impressions else 0.0
        rows.append(row)
    payload: dict[str, Any] = {"responseAggregationType": "byProperty"}
    if rows:
        payload["rows"] = rows
    return 200, payload


def inspect(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The fake ``urlInspection/index:inspect``."""
    site, url = body.get("siteUrl", ""), body.get("inspectionUrl", "")
    if site not in READABLE:
        return 403, {
            "error": {
                "code": 403,
                "message": "You do not own this site, or the inspected URL is not part of this property.",
                "status": "PERMISSION_DENIED",
            }
        }
    result = INSPECTIONS.get(url)
    if result is None:
        return 400, {
            "error": {
                "code": 400,
                "message": "The inspected URL is not part of this property.",
                "status": "INVALID_ARGUMENT",
            }
        }
    return 200, {"inspectionResult": result}


def route(
    method: str, raw_path: str, query: dict[str, list[str]], body: dict[str, Any] | None
) -> tuple[int, dict[str, Any]]:
    """Dispatch one request. ``raw_path`` is still percent-encoded."""
    with _lock:
        REQUESTS.append((method, unquote(raw_path), query, body))
    if raw_path == "/webmasters/v3/sites" and method == "GET":
        return 200, {"siteEntry": SITES}
    if m := re.fullmatch(r"/webmasters/v3/sites/([^/]+)/sitemaps", raw_path):
        site = unquote(m.group(1))
        if method != "GET":
            return _error(405, "methodNotAllowed", "read-only fake")
        if site in THROTTLED:
            return _error(403, "quotaExceeded", "Quota exceeded")
        if site not in READABLE:
            return _forbidden(site)
        if query.get("sitemapIndex"):
            return 200, {"sitemap": INDEX_CHILDREN}
        found = SITEMAPS[site]
        return 200, ({"sitemap": found} if found else {})
    if m := re.fullmatch(r"/webmasters/v3/sites/([^/]+)/searchAnalytics/query", raw_path):
        site = unquote(m.group(1))
        if method != "POST":
            return _error(405, "methodNotAllowed", "query is a POST")
        if site in THROTTLED:
            return _error(403, "quotaExceeded", "Quota exceeded")
        if site not in READABLE:
            return _forbidden(site)
        return search_analytics(body or {})
    if raw_path == "/v1/urlInspection/index:inspect" and method == "POST":
        return inspect(body or {})
    return _error(404, "notFound", raw_path)


def _unauthenticated() -> tuple[int, dict[str, Any]]:
    return 401, {
        "error": {
            "code": 401,
            "message": "Request had invalid authentication credentials.",
            "status": "UNAUTHENTICATED",
        }
    }


def handle(request: httpx.Request) -> httpx.Response:
    """``httpx.MockTransport`` handler."""
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        status, payload = _unauthenticated()
        return httpx.Response(status, json=payload)
    body = json.loads(request.content) if request.content else None
    raw_path = request.url.raw_path.decode().split("?", 1)[0]
    status, payload = route(request.method, raw_path, parse_qs(request.url.query.decode()), body)
    return httpx.Response(status, json=payload)


class _Handler(BaseHTTPRequestHandler):
    def _serve(self, method: str) -> None:
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            status, payload = _unauthenticated()
        else:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            parts = urlsplit(self.path)
            status, payload = route(method, parts.path, parse_qs(parts.query), body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, *_args: Any) -> None:
        pass


@contextmanager
def serve() -> Iterator[str]:
    """Run the fake on a free localhost port; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
