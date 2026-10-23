"""Session validation, secret scoping and expiry behavior. All values are fake."""

import pytest
from http_fakes import Response, Session
from yahoo_fantasy_football.http_client import YahooAccessDenied, fetch_text
from yahoo_fantasy_football.redzone_client import RedzoneClient
from yahoo_fantasy_football.session_auth import is_session_destination, normalize_session_cookie
from yahoo_fantasy_football.web_client import LeagueData

URL = "https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id=999999"
COOKIE = "A1=fake-session; A3=fake-other"


def test_pasted_cookie_header_drops_unneeded_browser_values():
    assert normalize_session_cookie("Cookie: A1=fake-session; tracking=discard; A3=fake-other") == COOKIE
    assert normalize_session_cookie("  ") == ""


@pytest.mark.parametrize(
    "raw",
    ["A1=fake\r\nHost: evil", "A1=fake\n", "A1=", "not a header", "tracking=only", "A1=x; A1=y", "A1=" + "x" * 32768],
    ids=["crlf", "newline", "empty-value", "malformed", "not-a-session", "duplicate", "too-long"],
)
def test_invalid_input_has_no_secret_in_error(raw):
    with pytest.raises(ValueError) as caught:
        normalize_session_cookie(raw)
    assert raw not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://relay-stream.sports.yahoo.com/nfl/games.txt",
        "https://lm-api-reads.fantasy.espn.com/anything",
        "https://pub-api.fantasysports.yahoo.com.evil.example/fantasy/v3/redzone/nfl",
        "http://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl",
        "https://pub-api.fantasysports.yahoo.com:444/fantasy/v3/redzone/nfl",
        "https://evil@pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl",
        "https://pub-api.fantasysports.yahoo.com/other",
    ],
)
async def test_session_is_never_sent_to_other_destinations(url):
    assert not is_session_destination(url)
    session = Session(Response())
    await fetch_text(session, url, headers={"Cookie": "must-not-leak"}, session_cookie=COOKIE)
    assert "Cookie" not in session.calls[0][1]["headers"]


async def test_league_and_historical_week_requests_receive_cookie_without_redirects():
    for url in (URL, URL + "&week=4"):
        assert is_session_destination(url)
        session = Session(Response())
        await fetch_text(session, url, headers={"User-Agent": "test"}, session_cookie=COOKIE)
        assert session.calls[0][1]["headers"]["Cookie"] == COOKIE
        assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403])
async def test_rejected_session_signals_renewal_without_secret_or_followup_request(status):
    session = Session(Response(status, "SECRET", {"Location": "https://example.com"}))
    with pytest.raises(YahooAccessDenied) as caught:
        await fetch_text(session, URL, headers={}, session_cookie=COOKIE)
    assert caught.value.session_supplied
    assert COOKIE not in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert len(session.calls) == 1


async def test_expired_session_is_not_hidden_by_last_good_scores():
    async def rejected(url):
        raise YahooAccessDenied(403, session_supplied=True)

    client = RedzoneClient(rejected, "999999")
    client._last_good = LeagueData(week=7, fetched_at=100)
    with pytest.raises(YahooAccessDenied):
        await client.async_refresh_or_stale(110)
