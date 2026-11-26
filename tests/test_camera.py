"""The camera entity: snapshot caching, graceful failure, MJPEG plumbing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CloudTransportError
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
    """Make the camera's snapshot write a real file, as OnTapo's does."""

    async def snapshot(path, seconds=4.0):
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    account.cameras[device_id].async_snapshot = AsyncMock(side_effect=snapshot)


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


async def test_temporary_files_are_cleaned_up(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """No camera footage should be left behind on disk after a snapshot."""
    await _setup(hass, entry_data, mock_account)
    paths: list[str] = []

    async def snapshot(path, seconds=4.0):
        paths.append(path)
        with open(path, "wb") as handle:
            handle.write(JPEG)
        return path

    mock_account.cameras[ptz_camera_info.device_id].async_snapshot = AsyncMock(
        side_effect=snapshot
    )
    await async_get_image(hass, "camera.garden")

    import os

    assert paths
    assert not any(os.path.exists(path) for path in paths)


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
