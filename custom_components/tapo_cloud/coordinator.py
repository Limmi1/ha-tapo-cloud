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
from .const import (
    DOMAIN,
    MEDIA_PROOF_SECONDS,
    OFFLINE_AFTER_FAILURES,
    UPDATE_INTERVAL,
)

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
        self._failures = 0
        self._last_good: dict[str, Any] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll, riding out brief cloud failures instead of flapping.

        The cloud's passthrough to a camera fails intermittently (HTTP 408)
        even while its video relay works. One failed poll must not make the
        camera unavailable: Home Assistant then revokes the entity's access
        token and every open live view goes blank. So a failure only counts
        once it repeats :data:`OFFLINE_AFTER_FAILURES` times in a row, and until
        then the last good state is kept.
        """
        try:
            state = await self._poll()
        except UpdateFailed:
            self._failures += 1
            if self._in_grace():
                _LOGGER.debug(
                    "poll %d failed for %s; keeping last good state",
                    self._failures, self.info.name,
                )
                return dict(self._last_good or {})
            raise

        if state.get("available"):
            self._failures = 0
            self._last_good = state
            return state

        self._failures += 1
        if self._in_grace():
            _LOGGER.debug(
                "%s reported offline (%d in a row); keeping last good state",
                self.info.name, self._failures,
            )
            return dict(self._last_good or {})
        return state

    async def _poll(self) -> dict[str, Any]:
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

    def _in_grace(self) -> bool:
        return self._last_good is not None and self._failures < OFFLINE_AFTER_FAILURES

    @property
    def available(self) -> bool:
        """Whether the camera is reachable.

        True when video or a snapshot from this physical camera (any of its
        lenses) arrived within :data:`MEDIA_PROOF_SECONDS`, which is proof it is
        online whatever the status poll says, or when it has been online and
        has not failed :data:`OFFLINE_AFTER_FAILURES` polls in a row. Every
        camera and lens follows this same rule.
        """
        if self.account.media_ok_within(self.info.stream_device_id, MEDIA_PROOF_SECONDS):
            return True
        return self._last_good is not None and self._failures < OFFLINE_AFTER_FAILURES
