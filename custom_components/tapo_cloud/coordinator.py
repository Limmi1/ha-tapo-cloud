"""Polling coordinator: one per camera, shared by every platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CameraInfo,
    CloudAuthError,
    CloudTransportError,
    TapoCloudAccount,
    TapoCloudCamera,
)
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class TapoCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps one camera's cloud-readable state fresh.

    Reconnection: a rejected token is refreshed once in-place and the poll is
    retried, so a camera that drops off the cloud (power cut, ISP outage at the
    camera's site) comes back on its own without the user touching anything.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        account: TapoCloudAccount,
        info: CameraInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {info.name}",
            update_interval=UPDATE_INTERVAL,
        )
        self.account = account
        self.info = info
        self.camera: TapoCloudCamera = account.camera(info)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.camera.async_poll()
        except CloudAuthError:
            # One in-place token refresh, then retry. If that fails too, the
            # entry needs re-authentication and HA will surface the repair.
            _LOGGER.debug("token rejected for %s; refreshing", self.info.device_id)
            await self.account.async_refresh_token()
            try:
                return await self.camera.async_poll()
            except CloudAuthError as err:
                raise UpdateFailed(f"authentication failed: {err}") from err
        except CloudTransportError as err:
            raise UpdateFailed(f"cloud unreachable: {err}") from err

    @property
    def available(self) -> bool:
        return bool(self.last_update_success and (self.data or {}).get("available"))
