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
import time

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CloudAuthError, CloudTransportError, OnTapoError
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
    "-map", "0:v:{stream}",  # the lens's own video stream; see relay_stream
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
        self._snapshot_failing = False
        self._refresh_task: asyncio.Task[bytes | None] | None = None

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
        """Return the last frame at once; fetch a newer one in the background.

        Pulling a new frame takes several seconds (relay session, a few seconds
        of video, an ffmpeg decode). Waiting for that on every refresh left
        dashboard cards blank or loading. Instead, once a frame exists, it is
        returned immediately ("stale while revalidate"); if it is older than
        ``snapshot_interval``, one background refresh starts, and the next
        request gets the new frame. Only the very first frame, when there is
        nothing to show yet, is waited for.
        """
        if self._snapshot is None:
            return await self._async_refresh_snapshot()
        if self._snapshot_stale and (
            self._refresh_task is None or self._refresh_task.done()
        ):
            self._refresh_task = self.hass.async_create_background_task(
                self._async_refresh_snapshot(),
                name=f"{self.entity_id} snapshot refresh",
            )
        return self._snapshot

    @property
    def _snapshot_stale(self) -> bool:
        return (time.monotonic() - self._snapshot_at) >= self._snapshot_interval

    async def _async_refresh_snapshot(self) -> bytes | None:
        """Fetch a new frame, keeping the last good one if that fails."""
        async with self._snapshot_lock:
            # Another caller may have refreshed while this one waited.
            if self._snapshot is not None and not self._snapshot_stale:
                return self._snapshot

            try:
                image = await self._async_decode_snapshot()
            except (CloudAuthError, CloudTransportError, OnTapoError, RuntimeError) as err:
                # Warn when snapshots start failing, so it shows in the normal
                # log; repeats go to debug so a dead camera doesn't flood it.
                log = _LOGGER.debug if self._snapshot_failing else _LOGGER.warning
                log("Snapshot failed for %s: %s", self.name or self.entity_id, err)
                self._snapshot_failing = True
                # Keep showing the last good frame rather than a broken image.
                return self._snapshot

            if self._snapshot_failing:
                _LOGGER.info("Snapshots working again for %s", self.name or self.entity_id)
                self._snapshot_failing = False
            self._snapshot = image
            self._snapshot_at = time.monotonic()
            return image

    async def _async_decode_snapshot(self) -> bytes:
        """Pull a few seconds of live relay video and decode one JPEG from it.

        Everything goes over pipes, so there is no file I/O at all. For a lens
        of a multi-lens camera, the handle streams the lens's own channel.
        """
        try:
            ffmpeg = get_ffmpeg_manager(self.hass).binary
        except (KeyError, AttributeError):
            ffmpeg = "ffmpeg"
        return await self.coordinator.camera.async_snapshot(
            ffmpeg=ffmpeg,
            seconds=DEFAULT_SNAPSHOT_SECONDS,
            resolution=self._resolution,
        )

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
            *_ffmpeg_args(self.coordinator.info.channel),
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


def _ffmpeg_args(stream: int) -> tuple[str, ...]:
    """ffmpeg arguments that transcode video stream ``stream`` to MJPEG."""
    return tuple(arg.format(stream=int(stream)) for arg in _FFMPEG_ARGS)

