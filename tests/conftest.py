"""Shared fixtures: hermetic process state, and the in-process fake Search Console.

The worker keeps two pieces of process-wide state — the HTTP connection pool
and the credential cache. Each is reset around every test so the order tests
run in cannot decide whether they pass.
"""

from __future__ import annotations

from collections.abc import Generator

import haybarn
import httpx
import pytest

from tests import fake_gsc
from vgi_google_search_console import auth, gsc_api, oauth
from vgi_google_search_console.settings import Settings

FAKE_BASE = "http://fake.test"


@pytest.fixture(scope="session", autouse=True)
def _install_vgi_extension() -> None:
    """Provision the SQL client's extension once, including on a fresh CI runner."""
    with haybarn.connect() as connection:
        connection.execute("INSTALL vgi FROM community")


@pytest.fixture(autouse=True)
def _hermetic() -> Generator[None]:
    """Reset process-wide caches, and never fall back to the developer's real ADC."""
    auth.permit_adc(False)
    oauth.clear_cache()
    oauth.permit_local(False)
    gsc_api.use_transport(None)
    auth.reset_cache()
    yield
    oauth.clear_cache()
    gsc_api.use_transport(None)
    auth.reset_cache()
    auth.permit_adc(True)
    oauth.permit_local(False)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> list:
    """Route the worker's HTTP through the in-process fake; returns its request log."""
    monkeypatch.setattr(gsc_api, "_RETRY_BASE_SECONDS", 0.0)
    fake_gsc.REQUESTS.clear()
    gsc_api.use_transport(httpx.MockTransport(fake_gsc.handle))
    return fake_gsc.REQUESTS


#: Settings pointing the API at the fake, as the equivalent ATTACH option would.
FAKE_SETTINGS = Settings(api_url=FAKE_BASE)


@pytest.fixture
def sess() -> gsc_api.Session:
    """A session with the token the fake accepts, aimed at the fake."""
    return gsc_api.Session(credentials=auth.load({auth.ACCESS_TOKEN: fake_gsc.TOKEN}), settings=FAKE_SETTINGS)
