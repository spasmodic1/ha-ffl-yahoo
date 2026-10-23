"""Run the actual config flow with a minimal HA shell and fake HTTP responses.

These tests cover the HTTP-to-form contract, not a full HA installation.
"""

import importlib.util
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from conftest import FIXTURES, REPO_ROOT
from http_fakes import Response, Session
from yahoo_fantasy_football.session_auth import CONF_SESSION_COOKIE


class ConfigFlowShell:
    """Only the HA form/entry API used by this integration."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_update_reload_and_abort(self, entry, *, data_updates):
        entry.data = {**entry.data, **data_updates}
        return {"type": "abort", "reason": "reauth_successful"}


@pytest.fixture
def flow_factory(monkeypatch):
    import homeassistant.config_entries as entries

    monkeypatch.setattr(entries, "ConfigFlow", ConfigFlowShell, raising=False)
    results = ModuleType("homeassistant.data_entry_flow")
    results.FlowResult = dict
    monkeypatch.setitem(sys.modules, results.__name__, results)
    http = ModuleType("homeassistant.helpers.aiohttp_client")
    monkeypatch.setitem(sys.modules, http.__name__, http)
    selector = ModuleType("homeassistant.helpers.selector")
    selector.TextSelector = lambda config: str
    selector.TextSelectorConfig = dict
    selector.TextSelectorType = SimpleNamespace(PASSWORD="password")
    monkeypatch.setitem(sys.modules, selector.__name__, selector)
    import homeassistant.helpers as helpers

    monkeypatch.setattr(helpers, "selector", selector, raising=False)
    spec = importlib.util.spec_from_file_location(
        "yahoo_fantasy_football._config_flow_test",
        REPO_ROOT / "custom_components/yahoo_fantasy_football/config_flow.py",
    )

    def create(response):
        session = Session(response)
        http.async_get_clientsession = lambda hass: session
        http.async_create_clientsession = lambda hass: session
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        flow = module.YahooFantasyFootballConfigFlow()
        flow.hass = object()
        return flow, session

    return create


@pytest.mark.parametrize("league", ["726304", "https://football.fantasysports.yahoo.com/f1/913531"])
async def test_yahoo_cookie_refusal_has_actionable_form_error(flow_factory, league):
    flow, session = flow_factory(Response(403, json.dumps({"error": {"description": "Unable to retrieve cookie."}})))
    result = await flow.async_step_user({"league_id": league})
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "yahoo_access_denied"}
    assert len(session.calls) == 1


async def test_server_failure_keeps_generic_connection_error(flow_factory):
    flow, _ = flow_factory(Response(503))
    result = await flow.async_step_user({"league_id": "726304"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_invalid_id_never_makes_http_request(flow_factory):
    flow, session = flow_factory(Response())
    result = await flow.async_step_user({"league_id": "not a league"})
    assert result["errors"] == {"base": "invalid_league_id"}
    assert session.calls == []


async def test_success_still_offers_real_teams_and_creates_entry(flow_factory):
    flow, session = flow_factory(Response(200, (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()))
    result = await flow.async_step_user({"league_id": "999999", "name": "My league"})
    assert result["step_id"] == "team"
    assert flow._teams
    team_id = next(iter(flow._teams))
    entry = await flow.async_step_team({"team_id": team_id})
    assert entry["type"] == "create_entry"
    assert entry["data"] == {"league_id": "999999", "name": "My league", "team_id": team_id}
    assert len(session.calls) == 1


async def test_session_saved_only_after_validating_league(flow_factory):
    flow, session = flow_factory(Response(200, (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()))
    result = await flow.async_step_user({"league_id": "999999", CONF_SESSION_COOKIE: "A1=fake-secret"})
    assert result["step_id"] == "team"
    assert "fake-secret" not in str(result)
    entry = await flow.async_step_team({})
    assert entry["data"][CONF_SESSION_COOKIE] == "A1=fake-secret"
    assert session.calls[0][1]["headers"]["Cookie"] == "A1=fake-secret"


async def test_rejected_session_is_not_echoed_back_in_form(flow_factory):
    flow, _ = flow_factory(Response(403))
    result = await flow.async_step_user({"league_id": "999999", CONF_SESSION_COOKIE: "A1=fake-secret"})
    assert result["errors"] == {"base": "session_rejected"}
    assert "fake-secret" not in str(result)


async def test_malformed_session_never_reaches_network(flow_factory):
    flow, session = flow_factory(Response())
    result = await flow.async_step_user({"league_id": "999999", CONF_SESSION_COOKIE: "A1=fake\r\nInjected: bad"})
    assert result["errors"] == {"base": "invalid_session_cookie"}
    assert session.calls == []


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_session_renewal_preserves_league_name_team_and_identity(flow_factory, source):
    flow, _ = flow_factory(Response(200, (FIXTURES / "yahoo_redzone_2026_w1.json").read_text()))
    entry = SimpleNamespace(data={"league_id": "999999", "name": "Keep me", "team_id": "2"})
    flow._get_reauth_entry = lambda: entry
    flow._get_reconfigure_entry = lambda: entry
    if source == "reauth":
        result = await flow.async_step_reauth(entry.data)
    else:
        result = await flow.async_step_reconfigure()
    assert result["step_id"] == "reauth_confirm"
    result = await flow.async_step_reauth_confirm({CONF_SESSION_COOKIE: "A1=new-fake-secret"})
    assert result["type"] == "abort"
    assert entry.data == {
        "league_id": "999999",
        "name": "Keep me",
        "team_id": "2",
        CONF_SESSION_COOKIE: "A1=new-fake-secret",
    }
