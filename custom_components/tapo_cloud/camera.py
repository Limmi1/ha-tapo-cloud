"""Live camera feeds over the Tapo cloud relay.

Home Assistant's `stream` component needs a URL it can hand to go2rtc, and the
relay offers none: the media arrives as MPEG-TS parts on a TLS socket we hold
open ourselves. So the feed is served the way HA serves any push-only source —
transcoded to multipart MJPEG by ffmpeg, which every dashboard card and the
mobile apps render natively.

Snapshots use the same relay, decoded to a single JPEG, and are cached for
`snapshot_interval` seconds so that a dashboard full of cards does not open one
relay session per card per refresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CloudTransportError, OnTapoError
from .const import (
    CONF_RESOLUTION,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_RESOLUTION,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_SECONDS,
    DOMAIN,
)
from .coordinator import TapoCloudCoordinator
from .entity import TapoCloudEntity

_LOGGER = logging.getLogger(__name__)

_MJPEG_BOUNDARY = "tapocloudframe"
# ffmpeg is told to expect MPEG-TS on stdin and emit MJPEG on stdout. The low
# analyzeduration/probesize keep first-frame latency down on a relay stream
# that starts mid-GOP.
_FFMPEG_ARGS = (
    "-hide_banner",
    "-loglevel", "error",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-analyzeduration", "1000000",
    "-probesize", "1000000",
    "-f", "mpegts",
    "-i", "pipe:0",
    "-an",
    "-f", "mpjpeg",
    "-q:v", "6",
    "-boundary_tag", _MJPEG_BOUNDARY,
    "pipe:1",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one camera entity per discovered camera."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TapoCloudCamera(coordinator, entry)
        for coordinator in data.coordinators.values()
    )


class TapoCloudCamera(TapoCloudEntity, Camera):
    """A Tapo camera reached entirely through the cloud relay."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(self, coordinator: TapoCloudCoordinator, entry: ConfigEntry) -> None:
        TapoCloudEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._entry = entry
        self._snapshot: bytes | None = None
        self._snapshot_at: float = 0.0
        self._snapshot_lock = asyncio.Lock()

    @property
    def _resolution(self) -> str:
        return self._entry.options.get(CONF_RESOLUTION, DEFAULT_RESOLUTION)

    @property
    def _snapshot_interval(self) -> float:
        return float(
            self._entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
        )

    @property
    def is_streaming(self) -> bool:
        return self.available

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a recent still frame, decoding a new one when the cache ages out."""
        async with self._snapshot_lock:
            fresh = (
                self._snapshot is not None
                and (time.monotonic() - self._snapshot_at) < self._snapshot_interval
            )
            if fresh:
                return self._snapshot

            try:
                image = await self._async_decode_snapshot()
            except (CloudTransportError, OnTapoError, RuntimeError) as err:
                _LOGGER.debug(
                    "snapshot failed for %s: %s", self.coordinator.info.device_id, err
                )
                # Serve the stale frame rather than a broken-image icon.
                return self._snapshot

            self._snapshot = image
            self._snapshot_at = time.monotonic()
            return image

    async def _async_decode_snapshot(self) -> bytes:
        """Pull a few seconds of live relay video and decode one JPEG from it."""
        loop = asyncio.get_running_loop()
        fd, path = await loop.run_in_executor(
            None, lambda: tempfile.mkstemp(suffix=".jpg", prefix="tapo_cloud_")
        )
        await loop.run_in_executor(None, os.close, fd)
        try:
            await self.coordinator.camera.async_snapshot(
                path, seconds=DEFAULT_SNAPSHOT_SECONDS
            )
            return await loop.run_in_executor(None, _read_file, path)
        finally:
            with contextlib.suppress(OSError):
                await loop.run_in_executor(None, os.unlink, path)

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Stream the live view as multipart MJPEG for as long as the client reads.

        The relay session, the ffmpeg process and the HTTP response are torn
        down together: whichever end goes away first stops the other two.
        """
        try:
            ffmpeg = get_ffmpeg_manager(self.hass).binary
        except (KeyError, AttributeError) as err:
            raise HomeAssistantError(
                "Tapo Cloud needs ffmpeg to serve a live feed. Install the "
                "FFmpeg add-on, or use the camera's snapshot image instead."
            ) from err

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            *_FFMPEG_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None

        response = web.StreamResponse(
            headers={
                "Content-Type": f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
                "Cache-Control": "no-store",
            }
        )

        feeder = asyncio.create_task(self._async_feed(process))
        try:
            await response.prepare(request)
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            _LOGGER.debug("mjpeg client disconnected from %s", self.entity_id)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await feeder
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    async def _async_feed(self, process: asyncio.subprocess.Process) -> None:
        """Pump relay MPEG-TS chunks into ffmpeg's stdin."""
        stdin = process.stdin
        assert stdin is not None
        try:
            async for chunk in self.coordinator.camera.iter_live_ts(
                resolution=self._resolution
            ):
                stdin.write(chunk)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg or the client went away first
        except (CloudTransportError, OnTapoError) as err:
            _LOGGER.warning(
                "live stream for %s ended: %s", self.coordinator.info.device_id, err
            )
        finally:
            with contextlib.suppress(Exception):
                stdin.close()


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
