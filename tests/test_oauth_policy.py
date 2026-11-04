"""Shared contract for credential selection across the Google workers."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as stream:
    PACKAGE = tomllib.load(stream)["project"]["name"].replace("-", "_")
settings = importlib.import_module(f"{PACKAGE}.settings")
oauth = importlib.import_module(f"{PACKAGE}.oauth")
worker = importlib.import_module(f"{PACKAGE}.worker")
Catalog = getattr(worker, "GA4Catalog", None) or worker.SearchConsoleCatalog


@pytest.mark.parametrize("options", [{}, {"credentials": "auto"}, {"credentials": "secret"}])
def test_browser_login_requires_explicit_oauth(
    options: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "authorize", lambda: pytest.fail("Unexpected browser authorization"))
    result = Catalog().catalog_attach(
        name=Catalog.catalog_name, options=options, data_version_spec=None, implementation_version=None
    )
    attached = settings.Settings.decode(result.attach_opaque_data)
    assert attached.credentials == options.get("credentials", "auto")
    assert attached.oauth_grant is None
    assert attached.may_use_adc == (attached.credentials == "auto")


@pytest.mark.parametrize("option", ["oauth_profile", "oauth_reauth"])
@pytest.mark.parametrize("mode", ["auto", "secret", "oauth"])
def test_legacy_profile_options_fail_before_sign_in(
    option: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "authorize", lambda: pytest.fail("Unexpected browser authorization"))
    with pytest.raises(ValueError, match="each ATTACH now signs in independently"):
        Catalog().catalog_attach(
            name=Catalog.catalog_name,
            options={"credentials": mode, option: "legacy"},
            data_version_spec=None,
            implementation_version=None,
        )
