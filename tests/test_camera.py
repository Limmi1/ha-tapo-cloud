"""The camera entity: snapshot caching, graceful failure, MJPEG plumbing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
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
    await hass.async_block_till_done()  # the refresh runs in the background
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


async def test_the_last_frame_is_shown_at_once_while_a_new_one_loads(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """A stale frame is returned immediately; the slow refresh lands later."""
    import asyncio

    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    camera = mock_account.cameras[ptz_camera_info.device_id]
    old_frame, new_frame = JPEG, b"\xff\xd8new-frame\xff\xd9"

    _writes_jpeg(mock_account, ptz_camera_info.device_id, old_frame)
    assert (await async_get_image(hass, "camera.garden")).content == old_frame

    release = asyncio.Event()

    async def slow_snapshot(**kwargs):
        await release.wait()  # the camera takes its time
        return new_frame

    camera.async_snapshot = AsyncMock(side_effect=slow_snapshot)

    # Answered straight away with the last frame, though the refresh is stuck.
    image = await asyncio.wait_for(async_get_image(hass, "camera.garden"), timeout=2)
    assert image.content == old_frame

    release.set()
    await hass.async_block_till_done()
    assert (await async_get_image(hass, "camera.garden")).content == new_frame


async def test_many_requests_start_only_one_background_refresh(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    import asyncio

    await _setup(hass, entry_data, mock_account, options={CONF_SNAPSHOT_INTERVAL: 0})
    camera = mock_account.cameras[ptz_camera_info.device_id]
    _writes_jpeg(mock_account, ptz_camera_info.device_id)
    await async_get_image(hass, "camera.garden")  # first frame

    release = asyncio.Event()

    async def slow_snapshot(**kwargs):
        await release.wait()
        return JPEG

    camera.async_snapshot = AsyncMock(side_effect=slow_snapshot)
    for _ in range(10):  # a dashboard full of cards, all asking at once
        await async_get_image(hass, "camera.garden")
    release.set()
    await hass.async_block_till_done()
    assert camera.async_snapshot.await_count == 1


async def test_the_very_first_frame_is_waited_for(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """With nothing to show yet, the first request waits for a real frame."""
    await _setup(hass, entry_data, mock_account)
    _writes_jpeg(mock_account, ptz_camera_info.device_id)
    assert (await async_get_image(hass, "camera.garden")).content == JPEG


# -- live view: cached frame first, scale and frame-rate caps (0.1.8) -----------

def test_live_filters_are_added_only_when_configured():
    from custom_components.tapo_cloud.camera import _ffmpeg_args

    args = _ffmpeg_args(1, 1280, 5)
    vf = args[args.index("-vf") + 1]
    assert vf == "fps=5,scale=min(1280\\,iw):-2"
    # The filter sits after the input and stream selection, before the output.
    assert args.index("-map") < args.index("-vf") < args.index("mpjpeg")
    assert "-vf" not in _ffmpeg_args(0, 0, 0)
    assert _ffmpeg_args(0, 0, 3)[_ffmpeg_args(0, 0, 3).index("-vf") + 1] == "fps=3"


async def test_live_scaling_and_frame_rate_with_real_ffmpeg():
    """1080p at 15 fps in; frames no wider than 1280 at about 5 fps out."""
    import io
    import shutil

    from PIL import Image

    from custom_components.tapo_cloud.camera import _MJPEG_BOUNDARY, _ffmpeg_args

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=15",
        "-t", "2", "-c:v", "libx264", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot encode a test stream")

    run = await asyncio.create_subprocess_exec(
        ffmpeg, *_ffmpeg_args(0, 1280, 5),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await run.communicate(ts)
    assert run.returncode == 0, err.decode()

    frames = [part for part in out.split(f"--{_MJPEG_BOUNDARY}".encode()) if b"\xff\xd8" in part]
    assert 8 <= len(frames) <= 12, len(frames)  # ~5 fps over 2 s, not ~30
    first = frames[0][frames[0].index(b"\xff\xd8"):]
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (1280, 720)


async def test_live_scaling_never_enlarges_a_smaller_stream():
    import io
    import shutil

    from PIL import Image

    from custom_components.tapo_cloud.camera import _ffmpeg_args

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
        "-t", "1", "-c:v", "libx264", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot encode a test stream")
    run = await asyncio.create_subprocess_exec(
        ffmpeg, *_ffmpeg_args(0, 1280, 5),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await run.communicate(ts)
    first = out[out.index(b"\xff\xd8"):]
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (640, 360)


def test_the_cached_frame_part_is_framed_like_ffmpeg_output():
    from custom_components.tapo_cloud.camera import _MJPEG_BOUNDARY, _mjpeg_part

    part = _mjpeg_part(JPEG)
    assert part.startswith(f"--{_MJPEG_BOUNDARY}\r\n".encode())
    assert f"Content-length: {len(JPEG)}\r\n\r\n".encode() in part
    assert part.endswith(JPEG + b"\r\n")


async def test_live_view_sends_the_last_frame_before_live_frames(
    hass: HomeAssistant, entry_data, mock_account
):
    """Opening live view shows the cached frame at once, then ffmpeg's output."""
    from custom_components.tapo_cloud.camera import _mjpeg_part

    await _setup(hass, entry_data, mock_account)
    entity = hass.data["camera"].get_entity("camera.garden")
    entity._snapshot = JPEG

    async def fake_stream(resolution="HD"):
        yield b"\x47ts"

    entity.coordinator.camera.iter_live_ts = fake_stream
    live = [b"--frame\r\n", b"\xff\xd8live\xff\xd9", b""]
    spawned: list[tuple] = []

    class FakeStdout:
        async def read(self, _size):
            return live.pop(0)

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
            return None

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        spawned.append(args)
        return FakeProcess()

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
        patch("custom_components.tapo_cloud.camera.asyncio.create_subprocess_exec", spawn),
        patch("custom_components.tapo_cloud.camera.web.StreamResponse", FakeResponse),
        patch("custom_components.tapo_cloud.camera.get_ffmpeg_manager") as manager,
    ):
        manager.return_value.binary = "ffmpeg"
        await entity.handle_async_mjpeg_stream(object())
        await asyncio.sleep(0)

    assert written[0] == _mjpeg_part(JPEG)
    assert written[1:] == [b"--frame\r\n", b"\xff\xd8live\xff\xd9"]
    # Defaults: 1280 px wide, 5 fps.
    vf = spawned[0][spawned[0].index("-vf") + 1]
    assert vf == "fps=5,scale=min(1280\\,iw):-2"


async def test_live_view_ffmpeg_produces_frames_with_default_settings():
    """Regression: before 0.1.8 live view's ffmpeg command produced no frames.

    "-fflags nobuffer" stopped piped MPEG-TS decoding, and the MJPEG encoder
    rejected limited-range video. Real ffmpeg, camera-like H.264 (no B-frames).
    """
    import shutil

    from custom_components.tapo_cloud.camera import _ffmpeg_args

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
        "-t", "2", "-c:v", "libx264", "-bf", "0", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot encode a test stream")
    for width, fps in ((0, 0), (1280, 5)):
        run = await asyncio.create_subprocess_exec(
            ffmpeg, *_ffmpeg_args(0, width, fps),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await run.communicate(ts)
        assert run.returncode == 0, err.decode()
        assert out.count(b"\xff\xd8\xff") >= 8, (width, fps, err.decode()[:200])

