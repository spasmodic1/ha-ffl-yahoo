"""Fetch Yahoo's GameChannel tier, with the failure handling that needs.

Parsing lives in :mod:`yahoo_redzone`; this module is only about *getting* the
payloads and deciding what to do when that goes wrong. The network call is
**injected** as a coroutine, so all of it is exercised in tests with a fake
fetcher and none of it needs aiohttp or a live Yahoo.

Why this replaced the HTML scraper
----------------------------------
:mod:`web_client` reads Yahoo's public fantasy *web pages*. That works only for
leagues whose privacy setting is public, and it costs one request per matchup.
This source is strictly better on both counts:

* **Three requests per poll for the whole league**, regardless of size — the
  league seed plus two league-wide live feeds — against 1 + one-per-matchup.
* **Private leagues work.** Verified against one. There is no login and no
  token involved; this is the same anonymous tier the GameChannel page uses.

The error taxonomy is deliberately shared with :mod:`web_client` so the
coordinator did not have to learn a second one. :class:`LeagueIsPrivate` is
kept importable from here but is never raised: privacy is simply not a
condition this tier has.

Politeness
----------
This is still a scraper against a site that did not invite us. Cadence is the
coordinator's job, but the shape is bounded here: three requests per refresh
plus one *conditional* request per live game's play-by-play, no per-matchup
fan-out, and no browser impersonation beyond a single ``User-Agent``. The
endpoints answer without one — it is sent so the traffic is identifiable rather
than to evade anything. If Yahoo starts refusing, the correct response is to
fetch less, not to evade harder.

The play feeds are the one thing polled every refresh, and they are polled with
``If-Modified-Since`` because the relay honours it (verified 2026-09-17: a real
``Last-Modified``, ``304`` on an unchanged file). A ``304`` is a couple of
hundred bytes, so asking every 10 s costs less than the old scheme of fetching
the whole feed whenever the games feed *looked* like a play had run — and it
stops missing plays. The games feed reflects a snap 2-30 s before the play's
text reaches the play feed (measured live, six plays), so a fetch triggered by
the games feed was usually too early, and nothing triggered a second one until
the next snap: the last-play line ran a play behind, and a touchdown whose text
landed between the score change and the PAT was never shown at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .http_client import YahooAccessDenied
from .web_client import (  # noqa: F401  (LeagueIsPrivate re-exported for callers)
    STALE_AFTER_SECONDS,
    FetchFailed,
    LeagueData,
    LeagueIsPrivate,
    YahooWebError,
)
from .yahoo_redzone import (
    league_from_payloads,
    league_name,
    parse_relay_players,
    redzone_url,
    relay_url,
    team_choices,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ``(url) -> body``. Unlike the HTML tier there is no login bounce to detect,
# so the fetcher does not have to report where it landed.
Fetcher = Callable[[str], Awaitable[str]]

# ``(url, if_modified_since) -> (body, last_modified)``, with ``body`` ``None``
# when the server answered ``304 Not Modified``. Optional: the config flow and
# the tests that only need the seed get by on the plain fetcher, and without
# this one the play feeds are simply fetched whole every time.
ConditionalFetcher = Callable[[str, str | None], Awaitable[tuple[str | None, str]]]

# A redzone payload for a real league runs to ~190 KB; the relay feeds are
# small but never empty, since each carries a comment header. Anything under
# this is a truncated response or an error page, not data.
MIN_BODY = 40

# How long a live feed's last good copy stands in for it after a dropped
# fetch, per feed. Both are held for the same reason — see :meth:`_live_feed`
# — but they go wrong at very different speeds.
#
# ``stats`` only ever accumulates, so a held copy is behind by whatever was
# scored in the gap and no more. A quarter of an hour of that is survivable,
# and far closer to the truth than the zero an empty body computes.
#
# ``games`` carries a running clock and the score beside it, which a reader
# checks against a television. Two minutes of a stopped clock still passes for
# "the poll is a little behind"; past that, holding is its own lie.
FEED_HOLD_SECONDS: dict[str, float] = {"stats": STALE_AFTER_SECONDS, "games": 120.0}

# How long the league seed is reused between refreshes.
#
# The three payloads are wildly different sizes — measured against a real
# league: seed 178 KB, stats 5.7 KB, games 2.1 KB — and they change at wildly
# different rates. The seed carries rosters, projections and league settings,
# which move when somebody edits a lineup; the two relays carry every number
# that changes during a game. Refetching the seed on the live cadence was
# spending 96% of the bandwidth on the part that had not changed, and that
# cost is what kept the live cadence slow.
#
# Caching it decouples the two: the live feeds can be polled several times a
# minute while the seed is refreshed every few minutes. The cost is that a
# lineup change takes up to this long to appear, which is the right trade
# during a game — points move constantly, lineups do not.
SEED_TTL_SECONDS = 180.0

# How long past its TTL an aged-out seed still stands in while a fresh one is
# fetched in the background (see :meth:`RedzoneClient.async_refresh_seed`).
#
# Fetching it inside the refresh made every eighteenth live poll wait on
# 178 KB before showing a single new point. A minute covers the live cadence
# — the first poll after the TTL serves the old seed and the next one finds
# the new — without letting a seed go stale for a whole near-game or idle
# interval: a poll that comes later than this fetches it in line, as before.
SEED_GRACE_SECONDS = 60.0

# How long the NFL player dictionary is reused when nothing says it has grown.
#
# It answers "who is [42654]". It was cached on the belief that the answer
# changes when somebody is signed, not during a game — but the dictionary is
# a running list of everyone who has recorded a stat this week, and on a
# Sunday morning it gains a name every few seconds (see
# ``parse_relay_players``). Under a pure age TTL a player's first touch named
# an id the cached copy could not resolve, and the play stayed unrenderable
# until the next refetch: half an hour of a down missing from the games
# card's list, and the last-play line sitting a play behind. So age is no
# longer the only trigger: an id the caller needs and the copy lacks is one
# too (``async_players``). This TTL is what remains for the quiet case.
PLAYERS_TTL_SECONDS = 1800.0

# The floor between two refetches of the dictionary prompted by an id it
# lacks. Bounds the cost when an id is never going to resolve (a tackler
# without a stat line is filtered out before it gets here, but the filter is
# a heuristic about Yahoo's feed, not a guarantee): one ~17 KB gzip fetch per
# this many seconds at worst. Short, because this is the wait a reader sees
# between a play landing and the down appearing in the list.
PLAYERS_RETRY_SECONDS = 30.0

# How often a play feed is fetched whole regardless of ``If-Modified-Since``.
#
# ``Last-Modified`` has one-second resolution and comes from whichever origin
# answered, so a ``304`` is trusted only for this long before an unconditional
# fetch confirms it. Cheap insurance: one full (gzip, ~5 KB) fetch per live
# game every minute and a half.
PLAYS_RECHECK_SECONDS = 90.0

# The floor between two asks for the same play feed. The coordinator asks once
# per poll and the games card asks again for each expanded game a moment later;
# the second ask is answered from the first's result rather than with a second
# round trip. Kept under ``coordinator.CHASE_SECONDS``, which re-reads a feed
# whose text is due every few seconds and must not be answered from cache.
PLAYS_MIN_INTERVAL_SECONDS = 2.0


@dataclass
class _PlaysCache:
    """One game's play feed as last seen, and when."""

    body: str
    last_modified: str
    fetched_at: float
    """Last time the server was asked, whether it answered 200 or 304."""
    loaded_at: float
    """Last time a full body arrived."""


class RedzoneClient:
    """Read one league over Yahoo's anonymous GameChannel tier."""

    def __init__(
        self,
        fetch: Fetcher,
        league_id: str | int,
        *,
        sport: str = "nfl",
        seed_ttl: float = SEED_TTL_SECONDS,
        fetch_if_modified: ConditionalFetcher | None = None,
    ) -> None:
        self._fetch = fetch
        self._fetch_if_modified = fetch_if_modified
        self.league_id = str(league_id)
        self.sport = sport
        self._seed_ttl = seed_ttl
        self._last_good: LeagueData | None = None
        # Parsed, not raw: the seed is re-read on every refresh, and parsing
        # 178 KB of JSON several times a minute in the event loop is a cost
        # worth paying exactly once per fetch.
        self._seed: dict | None = None
        self._seed_at: float = 0.0
        # ``Last-Modified`` of the games feed the last refresh read — what
        # :meth:`async_games_changed` asks the relay about.
        self._games_modified = ""
        self._players: dict[str, str] | None = None
        self._players_at: float = 0.0
        # ``{feed: (last body that arrived, when)}``, held over a failed
        # fetch of that feed — see :meth:`_live_feed`.
        self._live: dict[str, tuple[str, float]] = {}
        # Eight live games ask for the dictionary in one gather; the first
        # fetches, the rest wait and find it fresh. Without this, one lacking
        # id was eight simultaneous full fetches.
        self._players_lock = asyncio.Lock()
        self._plays: dict[str, _PlaysCache] = {}

    # -- internals ---------------------------------------------------------

    async def _get(self, url: str) -> str:
        try:
            body = await self._fetch(url)
        except YahooWebError:
            raise
        except Exception as err:  # any transport error is transient
            raise FetchFailed(f"fetching {url}: {err}") from err

        if not body or len(body) < MIN_BODY:
            raise FetchFailed(f"suspiciously short response from {url} ({len(body or '')}B)")
        return body

    async def _get_optional(self, url: str) -> str:
        """A live feed that may legitimately be missing.

        Between seasons, and in the hours before the first game of a week,
        Yahoo serves these empty or not at all. That is not a failure — it
        means nobody has scored yet, and the league seed alone still renders a
        complete scoreboard of zeroes.

        It cannot distinguish that from a fetch that failed, though, and for
        a live feed the two want opposite handling: see :meth:`_live_feed`,
        which every live body goes through before it is used.
        """
        try:
            return await self._get(url)
        except YahooWebError as err:
            _LOGGER.debug("live feed %s unavailable: %s", url, err)
            return ""

    def _live_feed(self, feed: str, body: str, now: float) -> str:
        """One live relay feed, holding its last good copy over a failed fetch.

        :meth:`_get_optional` cannot tell a feed Yahoo does not serve yet from
        one whose fetch just failed — both arrive as ``""``. Before the week's
        first kickoff the distinction does not matter. Mid-game it decides
        whether the card tells the truth, and each feed lies differently:

        * **stats** — every player's points are COMPUTED from it, so an empty
          body scores the whole league zero. One dropped request read as every
          live player losing their game at once, and the poll after it as every
          one of them scoring it all back in a single play (2026-09-24).
        * **games** — an empty body is no slate at all, which is not "no game
          is live" but "no idea". The NFL card's games vanish, every matchup
          reads final, and the cadence drops to the near-game interval, so the
          blank outlives by five minutes the request that caused it
          (2026-09-25, 02:57:44Z to 03:02:44Z exactly).

        The distinction the code can actually make: a feed Yahoo serves empty
        still carries its comment header, which clears :data:`MIN_BODY`. So
        ``""`` here means the fetch failed, never that the feed is empty — and
        the copy in hand is the best answer available.

        Past :data:`FEED_HOLD_SECONDS` for that feed it stops being an answer
        and the refresh fails, which hands the whole poll to
        :meth:`async_refresh_or_stale`: a coherent payload from a minute ago
        beats a fresh one with a hole in it, and past
        :data:`STALE_AFTER_SECONDS` even that gives up and says so.
        """
        if body:
            self._live[feed] = (body, now)
            return body
        held = self._live.get(feed)
        if held is None:
            return ""  # nothing has ever arrived — an empty feed is the truth
        last, arrived_at = held
        age = now - arrived_at
        if age > FEED_HOLD_SECONDS[feed]:
            raise FetchFailed(f"no {feed} feed for {age:.0f}s")
        _LOGGER.debug("%s feed unavailable; reusing the %.0fs-old copy", feed, age)
        return last

    # -- public API --------------------------------------------------------

    async def async_fetch_seed(self) -> str:
        """The league seed on its own — used by the config flow to validate."""
        return await self._get(redzone_url(self.league_id, self.sport))

    async def async_league_name(self) -> str:
        return league_name(await self.async_fetch_seed(), self.league_id)

    async def async_team_choices(self) -> dict[str, str]:
        """``{team_id: name}`` for the config flow's team picker."""
        return team_choices(await self.async_fetch_seed(), self.league_id)

    async def _seed_payload(self, now: float) -> dict:
        """The league seed, refetched only once it has aged out.

        See :data:`SEED_TTL_SECONDS` for why this is cached at all. Just past
        the TTL the old seed still serves, for :data:`SEED_GRACE_SECONDS`,
        while the caller refreshes it in the background.
        """
        if self._seed is not None and (now - self._seed_at) < self._seed_ttl + SEED_GRACE_SECONDS:
            return self._seed
        return await self._fetch_seed_payload(now)

    async def _fetch_seed_payload(self, now: float) -> dict:
        body = await self._get(redzone_url(self.league_id, self.sport))
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise FetchFailed(f"league {self.league_id} seed is not JSON: {err}") from err
        self._seed, self._seed_at = payload, now
        return payload

    def seed_due(self, now: float) -> bool:
        """Whether a cached seed has aged out and wants refetching."""
        return self._seed is not None and (now - self._seed_at) >= self._seed_ttl

    async def async_refresh_seed(self, now: float) -> None:
        """Refetch the seed off the refresh's path. A failure keeps the old one.

        The next refresh tries again; past the grace it fetches in line, and
        a failure there is the refresh's failure, as it always was.
        """
        try:
            await self._fetch_seed_payload(now)
        except YahooWebError as err:
            _LOGGER.debug("background seed refresh failed: %s", err)

    async def _get_games(self) -> str:
        """The games feed, keeping its ``Last-Modified`` for :meth:`async_games_changed`."""
        url = relay_url("games", self.sport)
        if self._fetch_if_modified is None:
            return await self._get_optional(url)
        try:
            body, last_modified = await self._fetch_if_modified(url, None)
        except Exception as err:  # a dropped live feed is held, see _live_feed
            _LOGGER.debug("live feed %s unavailable: %s", url, err)
            return ""
        if not body or len(body) < MIN_BODY:
            return ""
        self._games_modified = last_modified
        return body

    async def async_games_changed(self) -> bool:
        """Whether the games feed has moved since the last refresh read it.

        One conditional request: an unchanged feed answers ``304`` with no
        body (verified 2026-10-22), so asking every couple of seconds costs a
        few hundred bytes. ``False`` when there is nothing to compare against
        or the ask fails — the next scheduled refresh is never skipped, so a
        wrong ``False`` only costs the head start.
        """
        if self._fetch_if_modified is None or not self._games_modified:
            return False
        try:
            body, _ = await self._fetch_if_modified(relay_url("games", self.sport), self._games_modified)
        except Exception as err:  # the scheduled refresh will try in earnest
            _LOGGER.debug("games feed check failed: %s", err)
            return False
        return body is not None

    async def async_players(self, now: float, needed: Iterable[str] = ()) -> dict[str, str]:
        """``{player_id: name}`` for everyone who has played this week, cached.

        ``needed`` is every id the caller is about to look up (see
        ``play_ids_needed``). One the cached copy lacks means the dictionary
        has grown since it was read, and it is refetched — no sooner than
        :data:`PLAYERS_RETRY_SECONDS` after the last read, so an id that never
        resolves cannot turn every poll into a full fetch. Without ``needed``
        only age decides, which is right for a caller with no text in hand.

        The same object comes back until a refetch replaces it, so a caller
        can tell "still the copy I rendered with" by identity.

        Best-effort: a play description with unresolved ids degrades to a
        shorter sentence, which is a far better outcome than failing a refresh
        over it.
        """
        async with self._players_lock:
            cached = self._players
            if cached is not None:
                age = now - self._players_at
                lacking = any(pid not in cached for pid in needed)
                if age < PLAYERS_TTL_SECONDS and not (lacking and age >= PLAYERS_RETRY_SECONDS):
                    return cached
            body = await self._get_optional(relay_url("players", self.sport))
            if not body:
                return cached or {}
            self._players, self._players_at = parse_relay_players(body), now
            return self._players

    async def async_plays(self, plays_id: str, now: float) -> str:
        """One game's play-by-play feed, or ``""`` if it is not being served.

        This is the feed whose *text* changes — Yahoo posts a terse description
        first and revises it in place — so it is never served from a cache by
        age. It is served from the cache when the server says the file has
        not changed (``304``), which is what makes asking on every poll
        affordable. See the module docstring for why every poll.

        ``now`` is the poll's timestamp. Two callers in one poll — the fantasy
        enrichment and the games card's last play — share one request.
        """
        if not plays_id:
            return ""
        url = relay_url(f"plays-{plays_id}", self.sport)
        if self._fetch_if_modified is None:
            return await self._get_optional(url)

        cached = self._plays.get(plays_id)
        if cached is not None and now - cached.fetched_at < PLAYS_MIN_INTERVAL_SECONDS:
            return cached.body

        since = None
        if cached is not None and now - cached.loaded_at < PLAYS_RECHECK_SECONDS:
            since = cached.last_modified or None
        try:
            body, last_modified = await self._fetch_if_modified(url, since)
        except Exception as err:  # a missing feed is cosmetic, never fatal
            _LOGGER.debug("live feed %s unavailable: %s", url, err)
            return cached.body if cached is not None else ""

        if body is None:  # 304: what we have is what there is
            if cached is None:
                return ""
            cached.fetched_at = now
            return cached.body
        if len(body) < MIN_BODY:
            _LOGGER.debug("suspiciously short response from %s (%dB)", url, len(body))
            return cached.body if cached is not None else ""
        self._plays[plays_id] = _PlaysCache(body, last_modified, now, now)
        return body

    async def async_refresh(self, now: float) -> LeagueData:
        """One poll: the two live feeds, plus the league seed when it is due.

        ``now`` is supplied by the caller — nothing in this module reads a
        clock, which keeps refresh behaviour deterministic under test.
        """
        # Concurrent, not sequential: three independent endpoints, and this
        # runs inside the refresh the card is waiting on, so serialising them
        # was three round trips of pure latency on every live poll. The seed is
        # usually a cache hit and returns without any IO at all.
        seed, stats, games = await asyncio.gather(
            self._seed_payload(now),
            self._get_optional(relay_url("stats", self.sport)),
            self._get_games(),
        )
        stats = self._live_feed("stats", stats, now)
        games = self._live_feed("games", games, now)

        try:
            data = league_from_payloads(seed, stats, games, self.league_id, now)
        except (ValueError, KeyError, TypeError) as err:
            # A seed that will not parse into a league is worse than no cache:
            # every subsequent poll would fail the same way until the TTL ran
            # out. Drop it so the next refresh fetches a fresh one.
            self._seed = None
            raise FetchFailed(f"could not read league {self.league_id}: {err}") from err

        if not data.standings:
            raise FetchFailed(f"no matchups in league {self.league_id}'s week {data.week}")

        self._last_good = data
        return data

    async def async_refresh_or_stale(self, now: float) -> LeagueData:
        """Refresh, falling back to the last good payload on a transient failure.

        Serving four-minute-old scores beats emptying the card mid-game; past
        :data:`STALE_AFTER_SECONDS` it stops pretending and raises.
        """
        try:
            return await self.async_refresh(now)
        except YahooAccessDenied:
            # Rejected credentials need reauthentication, not a stale-data
            # fallback which would hide the refusal from the coordinator.
            raise
        except YahooWebError as err:
            stale = self._last_good
            if stale is None:
                raise
            age = now - stale.fetched_at
            if age > STALE_AFTER_SECONDS:
                raise FetchFailed(f"no fresh data for {age:.0f}s: {err}") from err
            _LOGGER.debug("serving %.0fs-old data after a failed refresh: %s", age, err)
            return stale
