"""Read-only cloud state: privacy mode, motion detection, SD card health.

These are sensors rather than switches because TP-Link's cloud gateway accepts
reads but rejects every `set*` method before the camera sees it (see
docs/VERIFICATION.md). Writes are offered as services, which can report the
rejection, instead of as toggles that would silently snap back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TapoCloudCoordinator
from .entity import TapoCloudEntity
from .parsers import (
    motion_detection_enabled,
    privacy_enabled,
    sd_card_problem,
)


@dataclass(frozen=True, kw_only=True)
class TapoCloudBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Tapo Cloud binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    requires_sd_card: bool = False


SENSORS: tuple[TapoCloudBinarySensorDescription, ...] = (
    TapoCloudBinarySensorDescription(
        key="privacy_mode",
        translation_key="privacy_mode",
        value_fn=privacy_enabled,
    ),
    TapoCloudBinarySensorDescription(
        key="motion_detection",
        translation_key="motion_detection",
        value_fn=motion_detection_enabled,
    ),
    TapoCloudBinarySensorDescription(
        key="sd_card",
        translation_key="sd_card",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=sd_card_problem,
        requires_sd_card=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the read-only cloud state sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    entities: list[TapoCloudBinarySensor] = []
    for coordinator in data.coordinators.values():
        for description in SENSORS:
            if description.requires_sd_card and not coordinator.info.has_sd_card:
                continue
            entities.append(TapoCloudBinarySensor(coordinator, description))
    async_add_entities(entities)


class TapoCloudBinarySensor(TapoCloudEntity, BinarySensorEntity):
    """One read-only piece of cloud-visible camera state."""

    entity_description: TapoCloudBinarySensorDescription

    def __init__(
        self,
        coordinator: TapoCloudCoordinator,
        description: TapoCloudBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.info.device_id}-{description.key}"

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None
