"""Shared HTTP boundary for setup, live polling and player history.

Keep Yahoo's access refusals distinct from transport failures. Never include an
arbitrary upstream response body (which could contain session data) in errors.
It can send a manually supplied Yahoo session only to the league endpoint. It
does not automate sign-in, acquire browser cookies or retry rejected requests.
"""

from __future__ import annotations

import json
from typing import Any

from .session_auth import is_session_destination
from .web_client import YahooWebError


class YahooAccessDenied(YahooWebError):
    """Yahoo refused access; changing a league id need not resolve it."""

    def __init__(self, status: int, *, cookie_error: bool = False, session_supplied: bool = False) -> None:
        self.status = status
        self.cookie_error = cookie_error
        self.session_supplied = session_supplied
        if session_supplied:
            detail = "Yahoo rejected the session or account access. Renew the session and check league membership."
        else:
            detail = (
                "Yahoo could not retrieve a required cookie." if cookie_error else "Yahoo refused anonymous access."
            )
        super().__init__(f"Yahoo access denied (HTTP {status}). {detail}")


def _cookie_error(body: str) -> bool:
    """Recognize the known error without echoing arbitrary server text."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return False
    return payload["error"].get("description") == "Unable to retrieve cookie."


async def fetch_text(
    session: Any,
    url: str,
    *,
    headers: dict[str, str],
    timeout: Any = None,
    allow_not_modified: bool = False,
    session_cookie: str = "",
) -> tuple[str | None, str]:
    """Read text and Last-Modified, preserving status-based access failures.

    Other HTTP errors retain the session's usual exception and are handled by
    the existing transient-error/backoff code. A 304 is accepted only for the
    caller that explicitly requests conditional responses.
    """
    request_headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
    send_session = bool(session_cookie) and is_session_destination(url)
    if send_session:
        request_headers["Cookie"] = session_cookie
    kwargs: dict[str, Any] = {"headers": request_headers}
    if send_session:
        # Explicit Cookie headers can otherwise follow redirects to unrelated
        # hosts. A sign-in redirect should request renewal, never forward it.
        kwargs["allow_redirects"] = False
    if timeout is not None:
        kwargs["timeout"] = timeout
    async with session.get(url, **kwargs) as response:
        if send_session and response.status in (301, 302, 303, 307, 308):
            raise YahooAccessDenied(response.status, session_supplied=True)
        if response.status in (401, 403):
            # Bound the read: a denied request may serve a large HTML page.
            chunks = bytearray()
            while len(chunks) < 4096:
                chunk = await response.content.read(4096 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            body = chunks.decode("utf-8", errors="replace")
            raise YahooAccessDenied(response.status, cookie_error=_cookie_error(body), session_supplied=send_session)
        if response.status == 304 and allow_not_modified:
            return None, ""
        response.raise_for_status()
        return await response.text(), response.headers.get("Last-Modified", "")
