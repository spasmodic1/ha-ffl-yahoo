"""Config entry diagnostics with session secrets explicitly redacted."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .session_auth import CONF_SESSION_COOKIE


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Expose metadata, never the Yahoo login session."""
    return async_redact_data(entry.as_dict(), {CONF_SESSION_COOKIE})
