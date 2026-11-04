"""Shared credential policy and portable OAuth grant format for Google workers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

AUTO = "auto"
SECRET_ONLY = "secret"
OAUTH = "oauth"
CREDENTIAL_MODES = (AUTO, SECRET_ONLY, OAUTH)


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """Refreshable authorization carried privately in a client's attach bytes.

    The versioned payload has no configurable scope or token endpoint. The
    random attachment ID isolates credential caches even when Google returns
    the same refresh token for two authorizations. Reprs never expose secrets.
    """

    attachment_id: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    version: int = 1

    @classmethod
    def decode(cls, raw: Any) -> OAuthGrant:
        """Validate an opaque grant without including its contents in errors."""
        fields = {"version", "attachment_id", "client_id", "client_secret", "refresh_token"}
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or type(raw.get("version")) is not int
            or raw["version"] != 1
            or any(not isinstance(raw[key], str) or not raw[key].strip() for key in fields - {"version"})
            or not re.fullmatch(r"[0-9a-f]{32}", raw["attachment_id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", raw["client_id"])
        ):
            raise ValueError("Invalid OAuth grant in attachment; detach and attach again to sign in")
        return cls(**raw)


def credential_mode(options: dict[str, Any]) -> str:
    """Validate credential selection and reject obsolete session options."""
    if "oauth_profile" in options or "oauth_reauth" in options:
        raise ValueError("OAuth profiles were removed; each ATTACH now signs in independently")
    mode = str(options.get("credentials") or AUTO).strip().lower()
    if mode not in CREDENTIAL_MODES:
        raise ValueError(f"ATTACH option credentials => {mode!r} is not one of {', '.join(CREDENTIAL_MODES)}")
    return mode
