"""The HTTP chokepoint: identifiers, retries, errors, and the four API calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests import fake_gsc
from vgi_google_search_console import auth, gsc_api
from vgi_google_search_console.settings import Settings


class TestIdentifiers:
    @pytest.mark.parametrize(
        "raw",
        [
            "sc-domain:example.com",
            "https://www.example.com/",
            "http://example.com/blog/",
            "https://sub.example.co.uk",
        ],
    )
    def test_both_property_forms_pass_through_unchanged(self, raw: str) -> None:
        assert gsc_api.site_url(f"  {raw} ") == raw

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("example.com", "use 'sc-domain:example.com'"),
            ("www.example.com", "'https://www.example.com/'"),
            ("", "not a Search Console property"),
            ("sc-domain:", "not a Search Console property"),
            ("sc-domain:https://example.com/", "not a Search Console property"),
            ("ftp://example.com/", "not a Search Console property"),
            ("android-app://com.example", "not a Search Console property"),
        ],
    )
    def test_bad_site_urls_explain_themselves(self, raw: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            gsc_api.site_url(raw)

    def test_page_urls_need_a_scheme(self) -> None:
        assert gsc_api.page_url(" https://www.example.com/a?b=c ") == "https://www.example.com/a?b=c"
        with pytest.raises(ValueError, match="including the"):
            gsc_api.page_url("/pricing")

    def test_property_type(self) -> None:
        assert gsc_api.property_type("sc-domain:example.com") == "domain"
        assert gsc_api.property_type("https://www.example.com/") == "url_prefix"
        assert gsc_api.property_type(None) is None


class TestCalls:
    def test_sites(self, fake: list, sess: gsc_api.Session) -> None:
        assert [s["siteUrl"] for s in gsc_api.sites(sess)] == [s["siteUrl"] for s in fake_gsc.SITES]

    def test_the_site_is_one_encoded_path_segment(self, sess: gsc_api.Session) -> None:
        seen: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path.decode())
            return httpx.Response(200, json={})

        gsc_api.use_transport(httpx.MockTransport(record))
        gsc_api.sitemaps("https://www.example.com/", sess)
        gsc_api.sitemaps("sc-domain:example.com", sess)
        assert seen == [
            "/webmasters/v3/sites/https%3A%2F%2Fwww.example.com%2F/sitemaps",
            "/webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps",
        ]

    def test_sitemaps(self, fake: list, sess: gsc_api.Session) -> None:
        found = gsc_api.sitemaps(fake_gsc.DOMAIN_SITE, sess)
        assert [s["path"] for s in found] == [s["path"] for s in fake_gsc.SITEMAPS[fake_gsc.DOMAIN_SITE]]
        assert gsc_api.sitemaps(fake_gsc.PREFIX_SITE, sess) == []

    def test_sitemap_index_is_a_query_parameter(self, fake: list, sess: gsc_api.Session) -> None:
        index = "https://www.example.com/sitemap.xml"
        assert len(gsc_api.sitemaps(fake_gsc.DOMAIN_SITE, sess, sitemap_index=index)) == 1
        assert fake[-1][2] == {"sitemapIndex": [index]}

    def test_search_analytics(self, fake: list, sess: gsc_api.Session) -> None:
        body = {"startDate": "2026-09-01", "endDate": "2026-09-28", "dimensions": [], "rowLimit": 10}
        [row] = gsc_api.search_analytics(fake_gsc.DOMAIN_SITE, body, sess)["rows"]
        assert row["clicks"] == sum(f["clicks"] for f in fake_gsc.FACTS)

    def test_inspect_url(self, fake: list, sess: gsc_api.Session) -> None:
        result = gsc_api.inspect_url(fake_gsc.DOMAIN_SITE, fake_gsc.PAGES[0], sess, language_code="en-US")
        assert result["indexStatusResult"]["verdict"] == "PASS"
        assert fake[-1][3] == {
            "inspectionUrl": fake_gsc.PAGES[0],
            "siteUrl": fake_gsc.DOMAIN_SITE,
            "languageCode": "en-US",
        }

    def test_a_bad_site_never_reaches_the_network(self, sess: gsc_api.Session) -> None:
        gsc_api.use_transport(httpx.MockTransport(lambda _r: pytest.fail("a request was sent")))
        with pytest.raises(ValueError, match="hostname"):
            gsc_api.search_analytics("example.com", {}, sess)


def _session() -> gsc_api.Session:
    return gsc_api.Session(
        credentials=auth.load({auth.ACCESS_TOKEN: "t"}), settings=Settings(api_url="http://fake.test")
    )


def _respond(*responses: httpx.Response) -> list[httpx.Request]:
    """Answer successive requests with ``responses``; returns the requests seen."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    gsc_api.use_transport(httpx.MockTransport(handler))
    return seen


def _old_style(status: int, reason: str, message: str = "nope") -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    )


class TestErrors:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gsc_api, "_RETRY_BASE_SECONDS", 0.0)

    def test_forbidden_says_how_to_get_access(self) -> None:
        _respond(_old_style(403, "forbidden", "User does not have sufficient permission for site"))
        with pytest.raises(gsc_api.SearchConsoleError, match="Users and permissions") as caught:
            gsc_api.sites(_session())
        assert caught.value.status == 403
        assert caught.value.reason == "forbidden"

    def test_forbidden_names_the_service_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sess = _session()
        monkeypatch.setattr(
            sess.credentials,
            "google",
            type("G", (), {"service_account_email": "sa@p-iam", "valid": True, "token": "t"})(),
        )
        _respond(_old_style(403, "forbidden"))
        with pytest.raises(gsc_api.SearchConsoleError, match="the service account sa@p-iam"):
            gsc_api.sites(sess)

    def test_unauthenticated_names_the_credential(self) -> None:
        _respond(httpx.Response(401, json={"error": {"message": "bad token", "status": "UNAUTHENTICATED"}}))
        with pytest.raises(gsc_api.SearchConsoleError, match=r"access token .* was rejected"):
            gsc_api.sites(_session())

    def test_a_disabled_api_says_which_to_enable(self) -> None:
        _respond(
            httpx.Response(
                403,
                json={
                    "error": {
                        "message": "Google Search Console API has not been used in project 1 before",
                        "status": "PERMISSION_DENIED",
                        "details": [{"reason": "SERVICE_DISABLED"}],
                    }
                },
            )
        )
        with pytest.raises(gsc_api.SearchConsoleError, match="Enable the 'Google Search Console API'"):
            gsc_api.sites(_session())

    def test_transient_statuses_are_retried(self) -> None:
        seen = _respond(httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"siteEntry": []}))
        assert gsc_api.sites(_session()) == []
        assert len(seen) == 3

    def test_a_403_rate_limit_is_retried_but_a_403_denial_is_not(self) -> None:
        seen = _respond(_old_style(403, "rateLimitExceeded"), httpx.Response(200, json={}))
        assert gsc_api.sites(_session()) == []
        assert len(seen) == 2
        seen = _respond(_old_style(403, "forbidden"))
        with pytest.raises(gsc_api.SearchConsoleError):
            gsc_api.sites(_session())
        assert len(seen) == 1

    def test_exhausted_quota_is_reported_as_quota_not_access(self) -> None:
        seen = _respond(_old_style(403, "quotaExceeded", "Quota exceeded"))
        with pytest.raises(gsc_api.SearchConsoleError, match="quota is exhausted") as caught:
            gsc_api.sites(_session())
        assert "Users and permissions" not in str(caught.value)
        assert len(seen) == 1

    @pytest.mark.parametrize(
        ("status", "reason", "expected"),
        [
            (403, "forbidden", True),
            (403, "PERMISSION_DENIED", True),
            (404, "notFound", True),
            (403, "quotaExceeded", False),
            (403, "rateLimitExceeded", False),
            (403, "SERVICE_DISABLED", False),
            (429, "RESOURCE_EXHAUSTED", False),
            (400, "invalid", False),
            (500, "", False),
            (0, "", False),
        ],
    )
    def test_only_a_refusal_of_one_site_may_be_stepped_over(
        self, status: int, reason: str, expected: bool
    ) -> None:
        error = gsc_api.SearchConsoleError("x", status=status, reason=reason)
        assert error.is_access_refusal is expected

    def test_persistent_failure_gives_up_with_the_last_error(self) -> None:
        seen = _respond(httpx.Response(429, json={"error": {"message": "slow down"}}))
        with pytest.raises(gsc_api.SearchConsoleError, match=r"429.*slow down"):
            gsc_api.sites(_session())
        assert len(seen) == gsc_api._RETRY_ATTEMPTS

    def test_a_non_json_success_is_an_error_with_context(self) -> None:
        _respond(httpx.Response(200, text="<html>gateway</html>"))
        with pytest.raises(gsc_api.SearchConsoleError, match="non-JSON 200"):
            gsc_api.sites(_session())

    def test_transport_failures_are_wrapped(self) -> None:
        def boom(_request: httpx.Request) -> Any:
            raise httpx.ConnectError("no route")

        gsc_api.use_transport(httpx.MockTransport(boom))
        with pytest.raises(gsc_api.SearchConsoleError, match="could not reach"):
            gsc_api.sites(_session())

    def test_the_token_is_sent_as_a_bearer_header(self) -> None:
        seen = _respond(httpx.Response(200, json={}))
        gsc_api.sites(_session())
        assert seen[0].headers["authorization"] == "Bearer t"
