"""Structural guard: this worker must never be able to change a Search Console account.

The API that lists sites and sitemaps is the same API that deletes them. The
worker is read-only by construction — one chokepoint with an allow-list, and a
read-only OAuth scope — and these tests fail the build if either stops being
true.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from vgi_google_search_console import auth, gsc_api
from vgi_google_search_console.settings import Settings

PACKAGE = Path(__file__).resolve().parent.parent / "vgi_google_search_console"

#: httpx calls that would issue a request outside the chokepoint.
_HTTP_CALLS = {"get", "post", "put", "patch", "delete", "stream", "send"}

SETTINGS = Settings(api_url="https://api.example")
SITE = "https%3A%2F%2Fwww.example.com%2F"


def _sources() -> dict[str, str]:
    return {path.name: path.read_text() for path in PACKAGE.rglob("*.py")}


class TestChokepoint:
    def test_only_gsc_api_touches_httpx(self) -> None:
        users = sorted(name for name, text in _sources().items() if "httpx" in text)
        assert users == ["gsc_api.py"], users

    def test_no_module_imports_another_http_client(self) -> None:
        for name, text in _sources().items():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    modules = (
                        [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    )
                    for module in modules:
                        assert not module.startswith(
                            ("requests", "urllib3", "aiohttp", "http.client", "googleapiclient")
                        ), f"{name} imports {module}"
                        assert module != "google.auth.transport.requests", (
                            f"{name} uses the requests transport"
                        )

    def test_gsc_api_issues_requests_only_from_the_chokepoints(self) -> None:
        """Every HTTP call in gsc_api lives in `_call` or the token transport.

        An HTTP call is a verb on ``httpx`` itself, on a name containing
        ``client``, or on ``shared_client()`` — which excludes ``dict.get``.
        """
        tree = ast.parse((PACKAGE / "gsc_api.py").read_text())

        def is_client(node: ast.expr) -> bool:
            if isinstance(node, ast.Name):
                return node.id == "httpx" or "client" in node.id
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "shared_client"
            )

        offenders = []
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _HTTP_CALLS | {"request"}
                    and is_client(node.func.value)
                ):
                    offenders.append((func.name, node.func.attr))
        assert offenders, "the guard found no HTTP calls at all, so it is not looking at the right thing"
        offenders = [f"{name}: .{attr}()" for name, attr in offenders if name not in {"_call", "__call__"}]
        assert offenders == [], offenders


class TestAllowList:
    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("GET", "https://api.example/webmasters/v3/sites"),
            ("GET", f"https://api.example/webmasters/v3/sites/{SITE}/sitemaps"),
            ("POST", f"https://api.example/webmasters/v3/sites/{SITE}/searchAnalytics/query"),
            ("POST", "https://api.example/v1/urlInspection/index:inspect"),
        ],
    )
    def test_reads_are_allowed(self, method: str, url: str) -> None:
        assert gsc_api._allowed(method, url, SETTINGS)

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            # Adding and removing a property.
            ("PUT", f"https://api.example/webmasters/v3/sites/{SITE}"),
            ("DELETE", f"https://api.example/webmasters/v3/sites/{SITE}"),
            # Submitting and deleting a sitemap.
            ("PUT", f"https://api.example/webmasters/v3/sites/{SITE}/sitemaps/sitemap.xml"),
            ("DELETE", f"https://api.example/webmasters/v3/sites/{SITE}/sitemaps/sitemap.xml"),
            ("POST", f"https://api.example/webmasters/v3/sites/{SITE}/sitemaps"),
            # POSTs that merely resemble the two read RPCs.
            ("POST", f"https://api.example/webmasters/v3/sites/{SITE}/searchAnalytics/queryX"),
            ("POST", "https://api.example/webmasters/v3/sites/a/b/searchAnalytics/query"),
            ("POST", "https://api.example/v1/urlInspection/index:inspectX"),
            ("POST", "https://api.example/v1/urlTestingTools/mobileFriendlyTest:run"),
            ("POST", "https://api.example/v1/urlInspection/index:inspect?alt=media"),
            # Another host, and a host that merely starts with the right one.
            ("GET", "https://evil.example/webmasters/v3/sites"),
            ("GET", "https://api.example.evil.test/webmasters/v3/sites"),
            ("GET", "https://api.example/v1/urlInspection/index:inspect"),
        ],
    )
    def test_everything_else_is_refused(self, method: str, url: str) -> None:
        assert not gsc_api._allowed(method, url, SETTINGS)

    def test_the_chokepoint_refuses_before_any_request(self) -> None:
        sess = gsc_api.Session(credentials=auth.load({auth.ACCESS_TOKEN: "t"}), settings=SETTINGS)
        gsc_api.use_transport(httpx.MockTransport(lambda _r: pytest.fail("a request was sent")))
        with pytest.raises(PermissionError):
            gsc_api._call("DELETE", f"https://api.example/webmasters/v3/sites/{SITE}", sess)

    def test_no_write_helpers_exist(self) -> None:
        """The module exposes list/query/inspect and nothing that could submit or delete."""
        public = {
            name
            for name, value in vars(gsc_api).items()
            if callable(value) and not name.startswith("_") and value.__module__ == gsc_api.__name__
        }
        for name in public:
            assert not name.startswith(("add", "delete", "remove", "submit", "put", "update")), name


class TestScope:
    def test_only_the_readonly_scope_is_ever_requested(self) -> None:
        assert auth.SCOPE == "https://www.googleapis.com/auth/webmasters.readonly"
        for name, text in _sources().items():
            for scope in ('auth/webmasters"', "auth/webmasters'", "auth/indexing"):
                assert scope not in text, f"{name} mentions {scope}"


class TestPathInjection:
    """A site URL from user SQL must never shape a request path beyond one segment."""

    HOSTILE = (
        "https://www.example.com/../../v1/urlInspection/index:inspect",
        "https://a/sitemaps/x?y=1#frag",
        "sc-domain:example.com/../x",
        "https://a/b%2F..%2Fc",
    )

    @pytest.mark.parametrize("value", HOSTILE)
    def test_a_site_url_stays_one_segment(self, value: str) -> None:
        encoded = gsc_api.segment(value)
        assert "/" not in encoded
        assert "?" not in encoded
        assert "#" not in encoded

    def test_a_hostile_site_cannot_turn_a_list_into_another_call(
        self, fake: list, sess: gsc_api.Session
    ) -> None:
        """Whatever the site string holds, the request made is still the sitemap list of one site."""
        hostile = "https://www.example.com/../../../v1/urlInspection/index:inspect"
        with pytest.raises(gsc_api.SearchConsoleError, match="403"):
            gsc_api.sitemaps(hostile, sess)
        [(method, path, _query, _body)] = fake
        assert method == "GET"
        assert path == f"/webmasters/v3/sites/{hostile}/sitemaps"
