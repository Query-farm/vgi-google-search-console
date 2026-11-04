"""Self-contained Query Farm browser sign-in pages.

This module and ``oauth_assets/`` are the canonical presentation shared with
vgi-google-search-console. Keep its bundled copies byte-identical when changing
the design; the workers remain independently installable without a new dependency.
Only publisher-owned service names and fixed outcome copy reach the template.
"""

from __future__ import annotations

import base64
from functools import cache
from html import escape
from importlib.resources import files
from string import Template
from typing import Literal

Service = Literal["google-analytics", "google-search-console"]
Outcome = Literal["received", "denied", "invalid"]

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_SERVICES: dict[Service, tuple[str, str]] = {
    "google-analytics": (
        "Google Analytics",
        "https://github.com/Query-farm/vgi-google-analytics#credentials",
    ),
    "google-search-console": (
        "Google Search Console",
        "https://github.com/Query-farm/vgi-google-search-console#credentials",
    ),
}

_OUTCOMES: dict[Outcome, tuple[str, str, str, str]] = {
    "received": (
        "Authorization received",
        "Google has sent its sign-in response. Your worker will finish connecting in your SQL session.",
        "Return to your SQL session",
        "You can close this tab. Check your SQL session for the connection result.",
    ),
    "denied": (
        "Sign-in not completed",
        "Google did not approve this sign-in request. This connection has not been set up.",
        "Try again from your SQL session",
        "Run ATTACH again when you’re ready to sign in and approve read-only access. You can close this tab.",
    ),
    "invalid": (
        "Response not accepted",
        "We couldn’t match this response to the sign-in request. This response has not been used.",
        "Return to the Google sign-in tab",
        "Continue there, or run ATTACH again from your SQL session if the request has expired.",
    ),
}

_ICONS: dict[Outcome, str] = {
    "received": '<path d="m7 12 3 3 7-7"/>',
    "denied": '<path d="M8 8l8 8M16 8l-8 8"/>',
    "invalid": '<path d="M12 7v6m0 4h.01"/>',
}


@cache
def _template() -> Template:
    """Embed fonts in the response: the short-lived listener serves no assets."""
    assets = files(__package__).joinpath("oauth_assets")
    source = assets.joinpath("page.html").read_text(encoding="utf-8")
    for name in ("petrona", "noto-sans"):
        font = base64.b64encode(assets.joinpath(f"{name}-latin.woff2").read_bytes()).decode("ascii")
        source = source.replace(f"__{name.upper().replace('-', '_')}_FONT__", font)
    return Template(source)


def render_page(service: Service, outcome: Outcome) -> bytes:
    """Render a fixed outcome without reflecting callback parameters or tokens."""
    name, documentation_url = _SERVICES[service]
    title, description, next_title, next_description = _OUTCOMES[outcome]
    return (
        _template()
        .substitute(
            service=escape(name),
            documentation_url=escape(documentation_url, quote=True),
            outcome=outcome,
            title=escape(title),
            description=escape(description),
            next_title=escape(next_title),
            next_description=escape(next_description),
            icon=_ICONS[outcome],
        )
        .encode("utf-8")
    )
