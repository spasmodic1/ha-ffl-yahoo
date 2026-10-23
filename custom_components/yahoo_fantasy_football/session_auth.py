"""Validate manually supplied Yahoo session cookies and restrict their destination."""

from __future__ import annotations

import re
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

CONF_SESSION_COOKIE = "yahoo_session_cookie"
_COOKIE_NAMES = frozenset({"A1", "A1S", "A3", "B", "GUC", "T", "Y"})


def normalize_session_cookie(raw: str) -> str:
    """Keep only Yahoo session cookies; reject malformed/header-injection input.

    Values are secrets. Errors deliberately never include the supplied text.
    A browser's Cookie request header may contain analytics/preferences too;
    these are discarded instead of retaining an entire browser cookie dump.
    """
    if not isinstance(raw, str) or len(raw) > 32768 or any(ord(c) < 32 or ord(c) > 126 for c in raw):
        raise ValueError("Invalid Yahoo session cookie header")
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if not raw:
        return ""
    parts = []
    seen = set()
    for part in raw.split(";"):
        if not part.strip():
            continue
        name, sep, value = part.strip().partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_!-]+", name) or not value:
            raise ValueError("Invalid Yahoo session cookie header")
        if name not in _COOKIE_NAMES:
            continue
        if name in seen:
            raise ValueError("Duplicate Yahoo session cookie")
        parsed = SimpleCookie()
        try:
            parsed.load(f"{name}={value}")
        except CookieError:
            raise ValueError("Invalid Yahoo session cookie header") from None
        if name not in parsed or len(parsed) != 1:
            raise ValueError("Invalid Yahoo session cookie header")
        parts.append(parsed[name].OutputString())
        seen.add(name)
    if not parts:
        raise ValueError("No supported Yahoo session cookies found")
    return "; ".join(parts)


def is_session_destination(url: str) -> bool:
    """Only the HTTPS league-seed endpoint receives the session secret.

    This covers current/historical weeks through query parameters. Relay,
    ESPN, image URLs, redirects and other Yahoo services do not need it.
    """
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "pub-api.fantasysports.yahoo.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/fantasy/v3/redzone/nfl"
        )
    except ValueError:
        return False
