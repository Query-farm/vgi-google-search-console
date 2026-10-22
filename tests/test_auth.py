"""Credential parsing and resolution."""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from vgi_google_search_console import auth


def _sa_key() -> str:
    """A syntactically valid service-account key with a freshly generated RSA key."""
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "demo",
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": "gsc-reader@demo.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


class TestSecretLookup:
    def test_found_by_type_whatever_the_secret_is_named(self) -> None:
        """The framework keys resolved secrets by *name*; lookup must go by type."""
        resolved = {"my_gsc": {"type": pa.scalar(auth.SECRET_TYPE), auth.ACCESS_TOKEN: pa.scalar("t")}}
        values = auth.secret_values(resolved)
        assert values is not None
        assert auth._text(values, auth.ACCESS_TOKEN) == "t"

    def test_a_secret_of_another_type_is_ignored(self) -> None:
        assert auth.secret_values({"s3": {"type": "s3", "key_id": "x"}}) is None

    def test_resolved_secrets_helper_is_used_when_present(self) -> None:
        class Resolved(dict[str, Any]):
            def of_type(self, kind: str) -> list[dict[str, Any]]:
                return [v for v in self.values() if v.get("type") == kind]

        class Accessor:
            def to_dict(self) -> Resolved:
                return Resolved(x={"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "tok"})

        assert auth.secret_values(Accessor()) == {"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "tok"}

    def test_none_is_no_secret(self) -> None:
        assert auth.secret_values(None) is None


class TestLoad:
    def test_access_token(self) -> None:
        creds = auth.load({auth.ACCESS_TOKEN: "ya29.x"})
        assert creds.headers(lambda **_: None) == {"authorization": "Bearer ya29.x"}

    def test_quota_project_is_sent(self) -> None:
        creds = auth.load({auth.ACCESS_TOKEN: "t", auth.QUOTA_PROJECT_ID: "billing-proj"})
        assert creds.headers(lambda **_: None)["x-goog-user-project"] == "billing-proj"

    def test_service_account_gets_the_readonly_scope(self) -> None:
        creds = auth.load({auth.SERVICE_ACCOUNT_JSON: _sa_key()})
        assert creds.google.service_account_email == "gsc-reader@demo.iam.gserviceaccount.com"
        assert auth.SCOPE in (creds.google.scopes or creds.google._scopes)
        assert "service account" in creds.source

    def test_user_grant(self) -> None:
        creds = auth.load({auth.CLIENT_ID: "cid", auth.CLIENT_SECRET: "sec", auth.REFRESH_TOKEN: "1//r"})
        assert creds.google.refresh_token == "1//r"
        assert creds.google.scopes == [auth.SCOPE]

    def test_user_grant_names_what_is_missing(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="'client_secret'"):
            auth.load({auth.CLIENT_ID: "cid", auth.REFRESH_TOKEN: "1//r"})

    def test_nothing_set(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="carries no credential"):
            auth.load({auth.CLIENT_ID: "cid"})

    def test_two_shapes_are_ambiguous(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="exactly one"):
            auth.load({auth.ACCESS_TOKEN: "t", auth.REFRESH_TOKEN: "r"})

    def test_service_account_json_must_parse(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="not valid JSON"):
            auth.load({auth.SERVICE_ACCOUNT_JSON: "{not json"})

    def test_an_oauth_client_file_is_not_a_service_account(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="must be a service-account key"):
            auth.load({auth.SERVICE_ACCOUNT_JSON: json.dumps({"installed": {"client_id": "x"}})})

    def test_refresh_failure_is_reported_cleanly(self) -> None:
        creds = auth.load({auth.CLIENT_ID: "c", auth.CLIENT_SECRET: "s", auth.REFRESH_TOKEN: "r"})

        def refuse(**_: Any) -> Any:
            raise RuntimeError("invalid_grant")

        with pytest.raises(auth.SearchConsoleAuthError, match="OAuth refresh token"):
            creds.headers(refuse)


class TestResolve:
    def test_equal_secrets_share_one_cached_credential(self) -> None:
        first = auth.resolve({"a": {"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "t"}})
        second = auth.resolve({"b": {"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "t"}})
        assert first is second

    def test_different_secrets_do_not_share(self) -> None:
        first = auth.resolve({"a": {"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "t1"}})
        second = auth.resolve({"a": {"type": auth.SECRET_TYPE, auth.ACCESS_TOKEN: "t2"}})
        assert first is not second

    def test_no_secret_and_no_adc_explains_how_to_fix_it(self) -> None:
        with pytest.raises(auth.SearchConsoleAuthError, match="CREATE SECRET"):
            auth.resolve({})

    def test_broken_secret_never_falls_back_to_adc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth.permit_adc(True)
        monkeypatch.setattr(auth, "_application_default", lambda: pytest.fail("fell back to ADC"))
        with pytest.raises(auth.SearchConsoleAuthError):
            auth.resolve({"x": {"type": auth.SECRET_TYPE, auth.CLIENT_ID: "only"}})

    def test_adc_used_when_both_caller_and_operator_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth.permit_adc(True)
        sentinel = auth.load({auth.ACCESS_TOKEN: "adc"})
        monkeypatch.setattr(auth, "_application_default", lambda: sentinel)
        assert auth.resolve(None, allow_adc=True) is sentinel

    def test_the_caller_can_refuse_adc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`credentials => 'secret'` at ATTACH."""
        auth.permit_adc(True)
        monkeypatch.setattr(auth, "_application_default", lambda: pytest.fail("fell back to ADC"))
        with pytest.raises(auth.SearchConsoleAuthError, match="does not fall back"):
            auth.resolve(None, allow_adc=False)

    def test_the_operator_overrides_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An HTTP server without --allow-adc: no client can opt into its identity."""
        auth.permit_adc(False)
        monkeypatch.setattr(auth, "_application_default", lambda: pytest.fail("fell back to ADC"))
        with pytest.raises(auth.SearchConsoleAuthError):
            auth.resolve(None, allow_adc=True)


class TestSecretSpec:
    def test_every_credential_field_is_redacted(self) -> None:
        redacted = {f.name for f in auth.SECRET_SPEC.schema if (f.metadata or {}).get(b"redact") == b"true"}
        assert redacted == {
            auth.SERVICE_ACCOUNT_JSON,
            auth.CLIENT_SECRET,
            auth.REFRESH_TOKEN,
            auth.ACCESS_TOKEN,
        }
