"""Privacy-mode switch.

Reading privacy mode over the cloud works; writing it is rejected by TP-Link's
gateway on every firmware tested so far. The switch is still provided because
the rejection is a server-side per-model schema check that differs by region
and model and may lift — and because a disabled control that explains itself is
more useful than a missing one. A failed write raises, so Home Assistant shows
the error and the entity snaps back to the camera's real state.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CloudSetterRejected, CloudTransportError, OnTapoError
from .const import DOMAIN
from .coordinator import TapoCloudCoordinator
from .entity import TapoCloudEntity
from .parsers import privacy_enabled

_LOGGER = logging.getLogger(__name__)

_REJECTED_MESSAGE = (
    "The Tapo cloud rejected this change before the camera received it "
    "(MODEL_SCHEMA_CHECK_FAILED). Settings cannot currently be written over "
    "the cloud — only read. Change it in the Tapo app, or use a local "
    "connection. Pan, tilt and presets are unaffected."
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a privacy switch for each camera that reports the feature."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TapoCloudPrivacySwitch(coordinator)
        for coordinator in data.coordinators.values()
    )


class TapoCloudPrivacySwitch(TapoCloudEntity, SwitchEntity):
    """Privacy mode (lens mask)."""

    _attr_translation_key = "privacy_mode"

    def __init__(self, coordinator: TapoCloudCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.info.device_id}-privacy_switch"

    @property
    def is_on(self) -> bool | None:
        return privacy_enabled(self.coordinator.data or {})

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self.coordinator.camera.async_set_privacy_mode(enabled)
        except CloudSetterRejected as err:
            raise HomeAssistantError(_REJECTED_MESSAGE) from err
        except (CloudTransportError, OnTapoError) as err:
            raise HomeAssistantError(f"Could not set privacy mode: {err}") from err
        await self.coordinator.async_request_refresh()
