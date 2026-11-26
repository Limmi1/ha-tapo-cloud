"""The camera entity: snapshot caching, graceful failure, MJPEG plumbing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CloudAuthError, CloudTransportError
from custom_components.tapo_cloud.const import (
    CONF_SNAPSHOT_INTERVAL,
    DOMAIN,
)

FROM_TOKENS = "custom_components.tapo_cloud.TapoCloudAccount.from_tokens"
JPEG = b"\xff\xd8\xff\xe0jpegbytes\xff\xd9"


async def _setup(hass, entry_data, account, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options=options or {},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    with patch(FROM_TOKENS, return_value=account):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _writes_jpeg(account, device_id, payload=JPEG):
    """Make the camera handle's snapshot return JPEG bytes, as it now does."""
    account.cameras[device_id].async_snapshot = AsyncMock(return_value=payload)


async def test_snapshot_returns_a_decoded_frame(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(hass, entry_data, mock_account)
    _writes_jpeg(mock_account, ptz_camera_info.device_id)

    image = await async_get_image(hass, "camera.garden")
    assert image.content == JPEG


async def test_snapshots_are_cached_so_a_dashboard_does_not_hammer_the_relay(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(
        hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 300}
    )
    _writes_jpeg(mock_account, ptz_camera_info.device_id)

    first = await async_get_image(hass, "camera.garden")
    second = await async_get_image(hass, "camera.garden")

    assert first.content == second.content == JPEG
    # One relay session, two dashboard reads.
    assert mock_account.cameras[ptz_camera_info.device_id].async_snapshot.await_count == 1


async def test_the_cache_expires(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    _writes_jpeg(mock_account, ptz_camera_info.device_id)

    await async_get_image(hass, "camera.garden")
    await async_get_image(hass, "camera.garden")
    assert mock_account.cameras[ptz_camera_info.device_id].async_snapshot.await_count == 2


async def test_a_failed_snapshot_serves_the_last_good_frame(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """A transient relay failure should not blank the dashboard card."""
    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    camera = mock_account.cameras[ptz_camera_info.device_id]

    _writes_jpeg(mock_account, ptz_camera_info.device_id)
    good = await async_get_image(hass, "camera.garden")
    assert good.content == JPEG

    camera.async_snapshot = AsyncMock(side_effect=CloudTransportError("relay down"))
    stale = await async_get_image(hass, "camera.garden")
    assert stale.content == JPEG


async def test_an_auth_refusal_also_serves_the_last_good_frame(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """A relay 401 that survives token renewal must not blank the card either."""
    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    camera = mock_account.cameras[ptz_camera_info.device_id]

    _writes_jpeg(mock_account, ptz_camera_info.device_id)
    assert (await async_get_image(hass, "camera.garden")).content == JPEG

    camera.async_snapshot = AsyncMock(side_effect=CloudAuthError("relay 401"))
    assert (await async_get_image(hass, "camera.garden")).content == JPEG


async def test_snapshot_failures_are_logged_visibly_once_then_recovery(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info, caplog
):
    """A failing camera warns once (not only at debug), then notes recovery."""
    import contextlib
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.tapo_cloud.camera")
    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    camera = mock_account.cameras[ptz_camera_info.device_id]

    camera.async_snapshot = AsyncMock(side_effect=CloudTransportError("relay preview HTTP 401"))
    for _ in range(3):
        # No good frame yet, so Home Assistant raises; only the logging matters.
        with contextlib.suppress(Exception):
            await async_get_image(hass, "camera.garden")

    failures = [r for r in caplog.records if "Snapshot failed" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert "relay preview HTTP 401" in failures[0].getMessage()

    _writes_jpeg(mock_account, ptz_camera_info.device_id)
    await async_get_image(hass, "camera.garden")
    assert any("working again" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


async def test_snapshot_uses_ha_ffmpeg_and_the_configured_quality(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """The snapshot goes through HA's own ffmpeg binary, with no file paths."""
    await _setup(hass, entry_data, mock_account, options={"resolution": "VGA"})
    _writes_jpeg(mock_account, ptz_camera_info.device_id)

    with patch("custom_components.tapo_cloud.camera.get_ffmpeg_manager") as manager:
        manager.return_value.binary = "/opt/ffmpeg"
        await async_get_image(hass, "camera.garden")

    call = mock_account.cameras[ptz_camera_info.device_id].async_snapshot.await_args
    assert call.args == ()
    assert call.kwargs["ffmpeg"] == "/opt/ffmpeg"
    assert call.kwargs["resolution"] == "VGA"


async def test_the_entity_reports_the_configured_stream_quality(
    hass: HomeAssistant, entry_data, mock_account
):
    await _setup(hass, entry_data, mock_account, options={"resolution": "VGA"})
    component = hass.data["camera"]
    entity = component.get_entity("camera.garden")
    assert entity._resolution == "VGA"


async def test_the_feeder_pumps_relay_chunks_into_ffmpeg(
    hass: HomeAssistant, entry_data, mock_account
):
    """The live path must forward every MPEG-TS chunk, then close ffmpeg's stdin."""
    await _setup(hass, entry_data, mock_account)
    entity = hass.data["camera"].get_entity("camera.garden")

    chunks = [b"\x47one", b"\x47two", b"\x47three"]

    async def fake_stream(resolution="HD"):
        for chunk in chunks:
            yield chunk

    written: list[bytes] = []

    class FakeStdin:
        def write(self, data):
            written.append(data)

        async def drain(self):
            return None

        def close(self):
            written.append(b"<closed>")

    class FakeProcess:
        stdin = FakeStdin()

    entity.coordinator.camera.iter_live_ts = fake_stream
    await entity._async_feed(FakeProcess())

    assert written == [*chunks, b"<closed>"]


async def test_a_relay_failure_mid_stream_closes_ffmpeg_cleanly(
    hass: HomeAssistant, entry_data, mock_account
):
    await _setup(hass, entry_data, mock_account)
    entity = hass.data["camera"].get_entity("camera.garden")

    async def failing_stream(resolution="HD"):
        yield b"\x47one"
        raise CloudTransportError("relay dropped")

    closed: list[bool] = []

    class FakeStdin:
        def write(self, data):
            return None

        async def drain(self):
            return None

        def close(self):
            closed.append(True)

    class FakeProcess:
        stdin = FakeStdin()

    entity.coordinator.camera.iter_live_ts = failing_stream
    # Must not propagate: the HTTP response is torn down by the caller instead.
    await entity._async_feed(FakeProcess())
    assert closed == [True]


async def test_the_mjpeg_endpoint_streams_ffmpeg_output_to_the_client(
    hass: HomeAssistant, entry_data, mock_account
):
    """End-to-end plumbing: relay chunks in, MJPEG bytes out, everything closed.

    This also covers the ffmpeg lookup, which goes through Home Assistant's own
    ffmpeg manager so the FFmpeg add-on's configured binary is honoured.
    """
    import asyncio

    await _setup(hass, entry_data, mock_account)
    entity = hass.data["camera"].get_entity("camera.garden")

    async def fake_stream(resolution="HD"):
        yield b"\x47ts-chunk"

    entity.coordinator.camera.iter_live_ts = fake_stream

    mjpeg_out = [b"--frame\r\n", b"\xff\xd8image\xff\xd9", b""]
    killed: list[bool] = []

    class FakeStdout:
        async def read(self, _size):
            return mjpeg_out.pop(0)

    class FakeStdin:
        def write(self, data):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class FakeProcess:
        stdin = FakeStdin()
        stdout = FakeStdout()

        def kill(self):
            killed.append(True)

        async def wait(self):
            return 0

    written: list[bytes] = []

    class FakeResponse:
        def __init__(self, headers=None):
            self.headers = headers or {}

        async def prepare(self, request):
            return None

        async def write(self, data):
            written.append(data)

        async def write_eof(self):
            return None

    with (
        patch(
            "custom_components.tapo_cloud.camera.asyncio.create_subprocess_exec",
            AsyncMock(return_value=FakeProcess()),
        ),
        patch(
            "custom_components.tapo_cloud.camera.web.StreamResponse",
            FakeResponse,
        ),
        patch(
            "custom_components.tapo_cloud.camera.get_ffmpeg_manager",
        ) as manager,
    ):
        manager.return_value.binary = "/usr/bin/ffmpeg"
        response = await entity.handle_async_mjpeg_stream(object())
        await asyncio.sleep(0)

    assert written == [b"--frame\r\n", b"\xff\xd8image\xff\xd9"]
    assert "multipart/x-mixed-replace" in response.headers["Content-Type"]
    assert killed == [True]


def test_live_view_transcodes_the_lens_own_video_stream():
    """Each lens is its own video stream; ffmpeg must map the right one."""
    from custom_components.tapo_cloud.camera import _ffmpeg_args

    for stream in (0, 1):
        args = _ffmpeg_args(stream)
        assert args[args.index("-map") + 1] == f"0:v:{stream}"
        # The map comes after the input it refers to.
        assert args.index("-map") > args.index("pipe:0")
