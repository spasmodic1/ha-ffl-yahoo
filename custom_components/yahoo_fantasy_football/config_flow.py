"""Config flow for Yahoo Fantasy Football.

A league is identified by the numeric id in its Yahoo URL, e.g. ``802904`` in
``football.fantasysports.yahoo.com/f1/802904``. That is all this source needs —
anonymous access is tried unless a Yahoo session cookie is supplied. OAuth and
password sign-in are not implemented. Expired sessions can be renewed through HA.

Two steps, because the second one can only be built from the first's answer:

1. ``user`` — take the league id and fetch it. Validating by actually fetching
   beats creating an entry that never produces data.
2. ``team`` — pick which team is yours, from the real names the fetch returned.
   Anonymous access reports ``isOwned: false`` for every team, so this cannot
   be detected and has to be asked. It is optional: skip it and you get the
   league scoreboard without the my-matchup sensor.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .const import (
    CONF_LEAGUE_ID,
    CONF_NAME,
    CONF_TEAM_ID,
    DEFAULT_NAME,
    DOMAIN,
)
from .http_client import YahooAccessDenied, fetch_text
from .redzone_client import USER_AGENT, RedzoneClient, YahooWebError
from .session_auth import CONF_SESSION_COOKIE, normalize_session_cookie
from .yahoo_web import extract_league_id

_LOGGER = logging.getLogger(__name__)

NO_TEAM = "none"


class YahooFantasyFootballConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Yahoo Fantasy Football league."""

    VERSION = 1

    def __init__(self) -> None:
        self._league_id: str = ""
        self._name: str = DEFAULT_NAME
        self._teams: dict[str, str] = {}
        self._session_cookie = ""
        self._renew_entry = None

    def _client(self, league_id: str) -> RedzoneClient:
        # Keep authenticated responses/cookies out of HA's shared session jar.
        session = async_create_clientsession(self.hass) if self._session_cookie else async_get_clientsession(self.hass)

        async def _fetch(url: str) -> str:
            body, _ = await fetch_text(
                session, url, headers={"User-Agent": USER_AGENT}, session_cookie=self._session_cookie
            )
            return body or ""

        return RedzoneClient(_fetch, league_id)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            league_id = extract_league_id(str(user_input[CONF_LEAGUE_ID]))
            if not league_id:
                errors["base"] = "invalid_league_id"
            else:
                try:
                    self._session_cookie = normalize_session_cookie(user_input.get(CONF_SESSION_COOKIE, ""))
                except ValueError:
                    return self._user_form({"base": "invalid_session_cookie"})
                await self.async_set_unique_id(f"web:{league_id}")
                self._abort_if_unique_id_configured()
                try:
                    seed = await self._client(league_id).async_fetch_seed()
                except YahooAccessDenied as err:
                    _LOGGER.debug("League %s access refused: %s", league_id, err)
                    errors["base"] = "session_rejected" if self._session_cookie else "yahoo_access_denied"
                except YahooWebError as err:
                    _LOGGER.debug("League %s failed validation: %s", league_id, err)
                    errors["base"] = "cannot_connect"
                else:
                    from .yahoo_redzone import league_name, team_choices

                    try:
                        self._teams = team_choices(seed, league_id)
                        found = league_name(seed, league_id)
                    except (ValueError, KeyError, TypeError) as err:
                        _LOGGER.debug("League %s payload unreadable: %s", league_id, err)
                        errors["base"] = "cannot_connect"
                    else:
                        self._league_id = league_id
                        # Yahoo knows the league's real name, so offer it rather
                        # than making the user retype it. An explicit entry wins.
                        typed = str(user_input.get(CONF_NAME) or "").strip()
                        self._name = typed or found or DEFAULT_NAME
                        return await self.async_step_team()

        return self._user_form(errors)

    def _user_form(self, errors: dict[str, str]) -> FlowResult:
        """Never echo an entered cookie back as a form default."""
        schema = vol.Schema(
            {
                vol.Required(CONF_LEAGUE_ID): str,
                vol.Optional(CONF_NAME): str,
                vol.Optional(CONF_SESSION_COOKIE): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_team(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose which team is yours, from the league's real team names."""
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_LEAGUE_ID: self._league_id,
                CONF_NAME: self._name,
            }
            if self._session_cookie:
                data[CONF_SESSION_COOKIE] = self._session_cookie
            team_id = str(user_input.get(CONF_TEAM_ID) or NO_TEAM)
            if team_id != NO_TEAM:
                data[CONF_TEAM_ID] = team_id
            return self.async_create_entry(title=self._name, data=data)

        choices = {NO_TEAM: "— none, league scoreboard only —", **self._teams}
        schema = vol.Schema({vol.Optional(CONF_TEAM_ID, default=NO_TEAM): vol.In(choices)})
        return self.async_show_form(
            step_id="team",
            data_schema=schema,
            description_placeholders={"league": self._name},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Request a replacement session after Yahoo rejects an existing one."""
        self._renew_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow adding/replacing a session without deleting a league entry."""
        self._renew_entry = self._get_reconfigure_entry()
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._session_cookie = normalize_session_cookie(user_input.get(CONF_SESSION_COOKIE, ""))
                if not self._session_cookie:
                    raise ValueError("Session required")
            except ValueError:
                errors["base"] = "invalid_session_cookie"
            else:
                league_id = str(self._renew_entry.data[CONF_LEAGUE_ID])
                try:
                    from .yahoo_redzone import team_choices

                    seed = await self._client(league_id).async_fetch_seed()
                    team_choices(seed, league_id)  # Validate this account can read the actual league.
                except YahooAccessDenied:
                    errors["base"] = "session_rejected"
                except (YahooWebError, ValueError, KeyError, TypeError):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        self._renew_entry, data_updates={CONF_SESSION_COOKIE: self._session_cookie}
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SESSION_COOKIE): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )
