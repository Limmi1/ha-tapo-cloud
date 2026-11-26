"""Diagnostics with every credential removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCOUNT_BASE,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_TERMINAL_UUID,
    CONF_TOKEN,
    DOMAIN,
)

TO_REDACT = {
    CONF_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TERMINAL_UUID,
    CONF_EMAIL,
    CONF_ACCOUNT_BASE,
    "password",
    "device_id",
    "mac",
    "ip",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "cameras": [
            {
                "model": coordinator.info.model,
                "firmware": coordinator.info.firmware,
                "has_ptz": coordinator.info.has_ptz,
                "has_sd_card": coordinator.info.has_sd_card,
                "feature_count": len(coordinator.info.features),
                "available": coordinator.available,
                "last_update_success": coordinator.last_update_success,
                "state_keys": sorted((coordinator.data or {}).keys()),
            }
            for coordinator in data.coordinators.values()
        ],
    }
