"""Query Farm browser sign-in, portable attachment grants and real SQL ATTACH."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests import fake_gsc
from vgi_google_search_console import auth, common, gsc_api, oauth, oauth_app, settings, worker

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
REFRESH_TOKEN = "test-refresh-token"


@pytest.fixture
def publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_app, "CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(oauth_app, "CLIENT_SECRET", CLIENT_SECRET)
    oauth.permit_local(True)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch, publisher: None):
    """Exercise the actual loopback callback and code exchange without contacting Google."""
    urls: list[dict[str, list[str]]] = []
    requests: list[dict[str, list[str]]] = []
    responses = []
    pool = ThreadPoolExecutor(max_workers=2)

    def browser(url: str) -> bool:
        assert url.startswith(oauth.AUTHORIZE_URL + "?")
        params = parse_qs(urlsplit(url).query)
        urls.append(params)
        callback = params["redirect_uri"][0]
        responses.append(
            pool.submit(httpx.get, callback, params={"state": params["state"][0], "code": "code"})
        )
        return True

    def token(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == auth.TOKEN_URI
        fields = parse_qs(request.content.decode())
        requests.append(fields)
        if fields["grant_type"] == ["authorization_code"]:
            verifier = fields["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
            assert challenge.decode() == urls[-1]["code_challenge"][0]
            assert fields["redirect_uri"] == urls[-1]["redirect_uri"]
            assert fields["code"] == ["code"]
        return httpx.Response(
            200,
            json={
                "access_token": "test-access-token",
                "refresh_token": REFRESH_TOKEN,
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": auth.SCOPE,
            },
        )

    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    gsc_api.use_transport(httpx.MockTransport(token))
    yield urls, requests
    pool.shutdown(wait=True)
    for response in responses:
        assert response.result().status_code == 200


class TestLogin:
    def test_attach_carries_refreshable_grant_privately(self, google: Any) -> None:
        result = worker.SearchConsoleCatalog().catalog_attach(
            name="gsc",
            options={"credentials": "oauth"},
            data_version_spec=None,
            implementation_version=None,
        )
        attached = settings.Settings.decode(result.attach_opaque_data)
        assert result.attach_opaque_data_required
        assert attached.oauth_grant is not None
        assert attached.oauth_grant.refresh_token == REFRESH_TOKEN
        assert attached.oauth_grant.client_id == CLIENT_ID
        assert attached.oauth_grant.client_secret == CLIENT_SECRET
        for secret in (CLIENT_SECRET, REFRESH_TOKEN, "test-access-token"):
            assert secret not in repr(attached)
            assert secret not in repr(attached.oauth_grant)
        assert b"test-access-token" not in result.attach_opaque_data
        assert auth.REFRESH_TOKEN not in {spec.name for spec in settings.OPTION_SPECS}
        urls, requests = google
        assert urls[0]["scope"] == [auth.SCOPE]
        assert urls[0]["client_id"] == [CLIENT_ID]
        assert urls[0]["code_challenge_method"] == ["S256"]
        assert urls[0]["access_type"] == ["offline"]
        assert "select_account" in urls[0]["prompt"][0]
        assert requests[0]["client_secret"] == [CLIENT_SECRET]
        params = SimpleNamespace(
            attach_opaque_data=result.attach_opaque_data,
            secrets={auth.SECRET_TYPE: {auth.ACCESS_TOKEN: "wrong-account"}},
        )
        credential = common.session(params).credentials
        for _ in range(2):
            assert credential.headers(gsc_api.AuthTransport()) == {
                "authorization": "Bearer test-access-token"
            }
        assert len(requests) == 2  # Code exchange, then one cached access-token refresh.
        assert auth._cache == {}  # Keep the OAuth attachment cache separate from supplied secrets.
        worker.SearchConsoleCatalog().catalog_detach(attach_opaque_data=result.attach_opaque_data)
        assert not oauth._cache
        # The client's bytes still reconstruct a credential after a cache eviction.
        assert common.session(params).credentials is not credential

    def test_each_attach_signs_in_and_evicts_only_its_cache(self, google: Any) -> None:
        first, second = oauth.authorize(), oauth.authorize()
        assert first != second
        assert len(google[0]) == 2
        oauth.credentials(first)
        other = oauth.credentials(second)
        oauth.forget(first)
        oauth.forget(first)
        assert first not in oauth._cache
        assert oauth.credentials(second) is other

    def test_failed_login_preserves_other_attachments(self, google: Any) -> None:
        grant = oauth.authorize()
        credential = oauth.credentials(grant)
        credential.headers(gsc_api.AuthTransport())
        gsc_api.use_transport(
            httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "invalid_grant"}))
        )
        with pytest.raises(auth.SearchConsoleAuthError, match="could not complete"):
            oauth.authorize()
        assert len(oauth._cache) == 1
        assert credential.headers(gsc_api.AuthTransport())["authorization"] == "Bearer test-access-token"

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({"access_token": "token", "token_type": "Bearer"}, "offline access"),
            (
                {"access_token": "token", "token_type": "Bearer", "scope": "unexpected"},
                "invalid or incomplete",
            ),
        ],
    )
    def test_incomplete_login_creates_no_grant(self, google: Any, payload: dict, message: str) -> None:
        gsc_api.use_transport(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
        with pytest.raises(auth.SearchConsoleAuthError, match=message):
            oauth.authorize()
        assert not oauth._cache

    def test_exchange_connection_failure_creates_no_grant(self, google: Any) -> None:
        def unavailable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        gsc_api.use_transport(httpx.MockTransport(unavailable))
        with pytest.raises(auth.SearchConsoleAuthError, match="Could not reach Google"):
            oauth.authorize()
        assert not oauth._cache

    def test_missing_app_registration_is_a_publisher_setup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oauth.permit_local(True)
        monkeypatch.setattr(oauth_app, "CLIENT_ID", "")
        monkeypatch.setattr(oauth_app, "CLIENT_SECRET", "")
        with pytest.raises(auth.SearchConsoleAuthError, match="Query Farm must register"):
            oauth.authorize()

    def test_cache_eviction_does_not_end_authorization(self, google: Any) -> None:
        grant = oauth.authorize()
        first = oauth.credentials(grant)
        first.headers(gsc_api.AuthTransport())
        oauth.clear_cache()
        second = oauth.credentials(grant)
        assert second is not first
        assert second.headers(gsc_api.AuthTransport())["authorization"] == "Bearer test-access-token"
        assert len(google[0]) == 1  # Reconstructing a grant never opens the browser.

    def test_expired_access_token_refreshes_with_same_grant(self, google: Any) -> None:
        from datetime import datetime, timedelta

        credential = oauth.credentials(oauth.authorize())
        credential.headers(gsc_api.AuthTransport())
        credential.google.expiry = datetime.now() - timedelta(hours=1)
        credential.headers(gsc_api.AuthTransport())
        assert len(google[0]) == 1
        assert len(google[1]) == 3
        assert google[1][-1]["refresh_token"] == [REFRESH_TOKEN]

    def test_cache_is_bounded(self, google: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oauth, "_CACHE_LIMIT", 2)
        first = oauth.authorize()
        second = replace(first, attachment_id="b" * 32)
        third = replace(first, attachment_id="c" * 32)
        oauth.credentials(first)
        oauth.credentials(second)
        oauth.credentials(third)
        assert len(oauth._cache) == 2
        assert first not in oauth._cache

    def test_refresh_errors_do_not_disclose_credentials(self, google: Any) -> None:
        credential = oauth.credentials(oauth.authorize())
        gsc_api.use_transport(
            httpx.MockTransport(
                lambda _: httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": REFRESH_TOKEN}
                )
            )
        )
        with pytest.raises(auth.SearchConsoleAuthError, match="Could not refresh") as caught:
            credential.headers(gsc_api.AuthTransport())
        assert REFRESH_TOKEN not in str(caught.value)
        assert caught.value.__suppress_context__


class TestCallback:
    @pytest.mark.parametrize(
        "query",
        [
            "state=wrong&code=secret-code",
            "state=expected&state=other&code=secret-code",
            "state=expected&code=a&code=b",
            "state=expected&code=a&error=access_denied",
            "state=expected&code=",
            "&".join(f"key{i}=x" for i in range(25)),
        ],
    )
    def test_invalid_response_does_not_complete_login(
        self, query: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with gsc_api.OAuthCallback("expected") as callback, ThreadPoolExecutor() as pool:
            pending = pool.submit(httpx.get, callback.redirect_uri + "?" + query)
            callback.handle_request()
            response = pending.result()
            assert response.status_code == 400
            assert callback.code == ""
            assert not callback.denied
            assert "secret-code" not in response.text
        assert "secret-code" not in capsys.readouterr().err

    def test_wrong_host_is_rejected(self) -> None:
        with gsc_api.OAuthCallback("state") as callback, ThreadPoolExecutor() as pool:
            pending = pool.submit(
                httpx.get,
                callback.redirect_uri,
                params={"state": "state", "code": "code"},
                headers={"Host": "attacker.example"},
            )
            callback.handle_request()
            assert pending.result().status_code == 400
            assert callback.code == ""

    def test_cancellation_is_reported(self) -> None:
        with gsc_api.OAuthCallback("expected") as callback, ThreadPoolExecutor() as pool:
            pending = pool.submit(
                httpx.get,
                callback.redirect_uri,
                params={"state": "expected", "error": "access_denied"},
            )
            with pytest.raises(ValueError, match="not approved"):
                callback.wait(2)
            assert pending.result().status_code == 200

    def test_timeout_closes_listener(self) -> None:
        with (
            gsc_api.OAuthCallback("expected") as callback,
            pytest.raises(TimeoutError, match="timed out"),
        ):
            callback.wait(0.01)
        assert callback.socket.fileno() == -1


class TestWorkerIndependence:
    def test_grant_survives_authorizing_process_exit(self) -> None:
        producer = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
from vgi_google_search_console import auth, oauth, oauth_app, worker
oauth.permit_local(True)
oauth_app.CLIENT_ID = "test-client.apps.googleusercontent.com"
oauth_app.CLIENT_SECRET = "test-client-secret"
oauth._browser_login = lambda client: {**client, auth.REFRESH_TOKEN: "test-refresh-token"}
result = worker.SearchConsoleCatalog().catalog_attach(
    name="gsc", options={"credentials": "oauth"},
    data_version_spec=None, implementation_version=None,
)
sys.stdout.buffer.write(result.attach_opaque_data)
""",
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert producer.returncode == 0, producer.stderr.decode()
        # The authorizing process is gone. Two new processes independently reuse
        # exactly the original bytes, supplied through stdin rather than argv.
        for _ in range(2):
            consumer = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs
import httpx
from vgi_google_search_console import auth, common, gsc_api, oauth
oauth.permit_local(True)
def browser(*args):
    raise AssertionError("Fresh workers must not sign in again")
oauth.webbrowser.open = browser
def request(req):
    if str(req.url) == auth.TOKEN_URI:
        fields = parse_qs(req.content.decode())
        assert fields["refresh_token"] == ["test-refresh-token"]
        assert fields["client_id"] == ["test-client.apps.googleusercontent.com"]
        assert fields["client_secret"] == ["test-client-secret"]
        assert fields["scope"] == [auth.SCOPE]
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
    assert str(req.url) == "https://searchconsole.googleapis.com/webmasters/v3/sites"
    assert req.headers["authorization"] == "Bearer fresh-token"
    return httpx.Response(200, json={"siteEntry": [{"siteUrl": "sc-domain:example.com"}]})
gsc_api.use_transport(httpx.MockTransport(request))
params = SimpleNamespace(attach_opaque_data=sys.stdin.buffer.read(), secrets=None)
assert gsc_api.sites(common.session(params)) == [{"siteUrl": "sc-domain:example.com"}]
""",
                ],
                input=producer.stdout,
                capture_output=True,
                check=False,
                timeout=15,
            )
            assert consumer.returncode == 0, consumer.stderr.decode()

    def test_two_accounts_cannot_share_tokens(self, publisher: None) -> None:
        def request(req: httpx.Request) -> httpx.Response:
            refresh = parse_qs(req.content.decode())["refresh_token"][0]
            return httpx.Response(200, json={"access_token": refresh, "expires_in": 3600})

        gsc_api.use_transport(httpx.MockTransport(request))
        first = settings.OAuthGrant("a" * 32, CLIENT_ID, CLIENT_SECRET, "first-user")
        # Even a reused attachment ID must not return another grant's cached token.
        second = replace(first, refresh_token="second-user")
        assert (
            oauth.credentials(first).headers(gsc_api.AuthTransport())["authorization"] == "Bearer first-user"
        )
        assert (
            oauth.credentials(second).headers(gsc_api.AuthTransport())["authorization"]
            == "Bearer second-user"
        )


class TestBoundaries:
    @pytest.mark.parametrize("mode", ["oauth", " OAUTH "])
    def test_custom_endpoint_cannot_receive_google_tokens(self, mode: str) -> None:
        with pytest.raises(ValueError, match="OAuth login"):
            settings.Settings.from_options({"credentials": mode, "api_url": "https://attacker.example"})
        with pytest.raises(ValueError, match="Invalid OAuth"):
            settings.Settings.decode(
                json.dumps({"credentials": mode, "api_url": "https://attacker.example"}).encode()
            )

    @pytest.mark.parametrize(
        "change",
        [
            {"version": 2},
            {"version": True},
            {"attachment_id": "bad"},
            {"client_id": "bad"},
            {"client_secret": ""},
            {"refresh_token": ""},
            {"refresh_token": 123},
            {"token_uri": "https://attacker.example"},
        ],
    )
    def test_invalid_grant_rejected_without_disclosing_tokens(self, change: dict) -> None:
        grant = asdict(settings.OAuthGrant("a" * 32, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN))
        with pytest.raises(ValueError, match="Invalid OAuth grant") as caught:
            settings.Settings.decode(
                json.dumps(
                    {
                        "credentials": "oauth",
                        "oauth_grant": {**grant, **change},
                    }
                ).encode()
            )
        assert REFRESH_TOKEN not in str(caught.value)

    @pytest.mark.parametrize("grant", [None, {}, [], "bad"])
    def test_missing_grant_never_falls_back_to_adc(self, grant: Any) -> None:
        with pytest.raises(ValueError, match="OAuth grant"):
            settings.Settings.decode(json.dumps({"credentials": "oauth", "oauth_grant": grant}).encode())

    def test_old_session_reference_requires_new_attach(self) -> None:
        with pytest.raises(ValueError, match="attach again"):
            settings.Settings.decode(
                json.dumps(
                    {
                        "credentials": "oauth",
                        "oauth_session": "1234:" + "a" * 64,
                    }
                ).encode()
            )

    def test_attach_options_cannot_supply_an_existing_grant(self) -> None:
        grant = asdict(settings.OAuthGrant("a" * 32, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN))
        attached = settings.Settings.from_options({"credentials": "oauth", "oauth_grant": grant})
        assert attached.oauth_grant is None

    @pytest.mark.parametrize("option", ["oauth_profile", "oauth_reauth"])
    def test_removed_profile_options_explain_new_behavior(self, option: str) -> None:
        with pytest.raises(ValueError, match="each ATTACH"):
            settings.Settings.from_options({"credentials": "oauth", option: "old"})

    def test_remote_access_is_rejected(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="local stdio"):
            oauth.authorize()
        with pytest.raises(auth.SearchConsoleAuthError, match="local stdio"):
            oauth.credentials(settings.OAuthGrant("a" * 32, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN))

    @pytest.mark.parametrize(
        "args",
        [["--http"], ["--tcp", "127.0.0.1:9000"], ["--unix=/tmp/test.sock"], ["--http", "--allow-adc"]],
    )
    def test_shared_cli_transports_disable_browser_login(
        self, args: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["worker", *args])
        monkeypatch.setattr(worker.SearchConsoleWorker, "main", lambda: None)
        worker.main()
        with pytest.raises(auth.SearchConsoleAuthError, match="local stdio"):
            oauth.authorize()
        assert auth.operator_permits_adc() == ("--allow-adc" in args)


@pytest.mark.parametrize("pool", [True, False])
def test_oauth_grant_works_through_real_sql_attach(tmp_path: Path, pool: bool) -> None:
    """SQL queries survive replacement workers and preserve independent attachments."""
    haybarn = pytest.importorskip("haybarn")
    root = Path(__file__).resolve().parent.parent
    events = tmp_path / "worker_events.txt"
    with fake_gsc.serve() as url:
        launcher = tmp_path / "oauth_worker.py"
        header = (root / "gsc_worker.py").read_text().split("# ///\n", maxsplit=1)[0] + "# ///\n"
        launcher.write_text(
            header
            + f"""
import json
import os
import sys
sys.path.insert(0, {str(root)!r})
from vgi_google_search_console import auth, gsc_api, oauth, oauth_app, settings, worker
settings.DEFAULT_API_URL = {url!r}
oauth_app.CLIENT_ID = {CLIENT_ID!r}
oauth_app.CLIENT_SECRET = {CLIENT_SECRET!r}
def event(kind):
    with open({str(events)!r}, "a") as out:
        out.write(kind + " " + str(os.getpid()) + "\\n")
def login(client):
    event("login")
    return {{**client, auth.REFRESH_TOKEN: {REFRESH_TOKEN!r}}}
oauth._browser_login = login
def refresh(self, *args, **kwargs):
    event("refresh")
    return gsc_api._AuthResponse(status=200, headers={{}}, data=json.dumps({{
        "access_token": {fake_gsc.TOKEN!r}, "expires_in": 3600, "token_type": "Bearer"
    }}).encode())
gsc_api.AuthTransport.__call__ = refresh
worker.main()
"""
        )
        with haybarn.connect() as con:
            for name in ("work_gsc", "personal_gsc"):
                con.execute(
                    f"ATTACH 'gsc' AS {name} (TYPE vgi, LOCATION 'uv run --exact --no-project {launcher}', "
                    f"credentials 'oauth', api_url '{url}', POOL {str(pool).lower()}, CACHE false)"
                )
            for _ in range(2):
                assert con.execute("SELECT count(*) FROM work_gsc.sites").fetchone() == (4,)
            con.execute("DETACH work_gsc")
            assert con.execute(
                "SELECT site_url FROM personal_gsc.sites WHERE property_type = 'domain'"
            ).fetchall() == [(fake_gsc.DOMAIN_SITE,)]
            con.execute("DETACH personal_gsc")
        recorded = [line.split() for line in events.read_text().splitlines()]
        login_pids = [pid for kind, pid in recorded if kind == "login"]
        refresh_pids = {pid for kind, pid in recorded if kind == "refresh"}
        assert len(login_pids) == 2  # Exactly one browser login per ATTACH.
        assert refresh_pids
        if not pool:
            assert refresh_pids.isdisjoint(login_pids)
            assert len(refresh_pids) >= 2
