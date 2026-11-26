"""Shared entity base: device registry identity and availability."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapoCloudCoordinator


class TapoCloudEntity(CoordinatorEntity[TapoCloudCoordinator]):
    """Base for every Tapo Cloud entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TapoCloudCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.info
        self._attr_unique_id = info.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info.device_id)},
            name=info.name,
            manufacturer="TP-Link",
            model=info.model or None,
            sw_version=info.firmware or None,
            configuration_url="https://www.tapo.com/",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.available
