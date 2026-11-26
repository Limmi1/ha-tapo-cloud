"""The temporary lens probe: variants, picture comparison and the service."""

from __future__ import annotations

import asyncio
import io
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from ontapo import RelayError
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CameraInfo
from custom_components.tapo_cloud.const import DOMAIN
from custom_components.tapo_cloud.probe import (
    app_path_url,
    compare_images,
    parse_video_streams,
    probe_channel,
    variants_for,
    video_streams,
)

PARENT = "FAKEDEVICE0123456789"


def _jpeg(colour: tuple[int, int, int], size=(320, 180), stripe=False) -> bytes:
    image = Image.new("RGB", size, colour)
    if stripe:
        for x in range(0, size[0], 40):
            for y in range(size[1]):
                for dx in range(20):
                    if x + dx < size[0]:
                        image.putpixel((x + dx, y), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


def test_variants_cover_the_candidate_requests_baseline_first():
    names = [v.name for v in variants_for(1)]
    assert names[0] == "baseline"
    assert set(names) == {
        "baseline", "frame-only", "app-path-ch0", "app-path", "app-path-frame0", "both-channels",
    }
    by_name = {v.name: v for v in variants_for(1)}
    assert by_name["baseline"].channels == (0,) and by_name["baseline"].app_path_channel is None
    assert by_name["app-path"].channels == (1,) and by_name["app-path"].app_path_channel == 1
    assert by_name["app-path-frame0"].channels == (0,)
    assert by_name["both-channels"].channels == (0, 1)
    assert by_name["both-channels"].decode_stream == 1


def test_app_path_url_keeps_the_relay_host_and_formats_like_the_app():
    url = app_path_url("https://relay-x.example.com/relayservice?opaque=blob", PARENT, "HD", 1)
    assert url == (
        f"https://relay-x.example.com/relayservice?deviceid={PARENT}"
        "&type=video&resolution=HD&channel=1"
    )


def test_parse_video_streams_lists_each_video_stream():
    stderr = (
        "Input #0, mpegts, from 'pipe:0':\n"
        "  Stream #0:0[0x100]: Video: h264 (High) ([27][0][0][0] / 0x001B), yuv420p, 2304x1296, 15 fps\n"
        "  Stream #0:1[0x101]: Audio: pcm_alaw, 8000 Hz, mono\n"
        "  Stream #0:2[0x102]: Video: hevc (Main), yuv420p, 1920x1080, 15 fps\n"
    )
    assert parse_video_streams(stderr) == ["h264 2304x1296", "hevc 1920x1080"]


def test_compare_images_tells_same_from_different():
    plain = _jpeg((90, 90, 90))
    assert compare_images(plain, _jpeg((92, 90, 88)))[1] == "same picture as lens 0"
    difference, verdict, size = compare_images(plain, _jpeg((10, 10, 10), stripe=True))
    assert verdict == "DIFFERENT picture" and difference > 25 and size == "320x180"


def _factory(outcomes):
    """A fake iter_relay_ts: each call takes the next outcome, records its args."""
    calls = []

    def fake(session, device_id, **kwargs):
        calls.append({"device_id": device_id, **kwargs})
        outcome = outcomes[len(calls) - 1]

        async def gen():
            if isinstance(outcome, Exception):
                raise outcome
            yield outcome

        return gen()

    return fake, calls


async def test_probe_channel_reports_each_variant(hass: HomeAssistant, tmp_path):
    lens0, lens1 = _jpeg((90, 90, 90)), _jpeg((10, 10, 10), stripe=True)
    fake, calls = _factory(
        [b"base", RelayError("relay preview HTTP 401"), b"c", b"d", b"e", b"f"]
    )
    frames = {b"base": lens0, b"c": lens0, b"d": lens1, b"e": lens0, b"f": lens0}

    async def fake_decode(video, *, ffmpeg, video_stream=0):
        return frames[video]

    with (
        patch("custom_components.tapo_cloud.probe.decode_first_frame", fake_decode),
        patch("custom_components.tapo_cloud.probe.video_streams", AsyncMock(return_value=["h264 2304x1296"])),
    ):
        results = await probe_channel(
            hass, MagicMock(), PARENT, 1, ffmpeg="ffmpeg", save_dir=tmp_path, stream_factory=fake
        )

    outcome = {r["variant"]: r["outcome"] for r in results}
    assert outcome["baseline"] == "reference picture (lens 0)"
    assert outcome["frame-only"].startswith("refused or failed: relay preview HTTP 401")
    assert outcome["app-path-ch0"] == "same picture as lens 0"
    assert outcome["app-path"] == "DIFFERENT picture"
    # The parent id is always the relay target; app-path variants swap the URL.
    assert {c["device_id"] for c in calls} == {PARENT}
    app_call = calls[3]
    assert app_call["channels"] == [1] and app_call["track_id"] == f"preview-{PARENT}-ch1"
    relay = MagicMock(relay_url="https://relay.example.com/relayservice?blob")
    assert app_call["relay_url"](relay).endswith("&channel=1")
    assert calls[0]["relay_url"] is None and calls[0]["track_id"] == f"preview-{PARENT}"
    # Pictures were saved for a human to look at; the refused variant has none.
    assert (tmp_path / "ch1_app-path.jpg").read_bytes() == lens1
    assert not (tmp_path / "ch1_frame-only.jpg").exists()


async def test_two_video_streams_are_found_and_the_second_decoded():
    """The both-channels case, end to end through a real ffmpeg."""
    from custom_components.tapo_cloud.api import decode_first_frame

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        "-f", "lavfi", "-i", "color=c=red:size=160x90:rate=10",
        "-t", "1", "-map", "0:v", "-map", "1:v", "-c:v", "libx264", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot build a two-stream test file")

    assert await video_streams(ts, ffmpeg) == ["h264 320x240", "h264 160x90"]
    second = await decode_first_frame(ts, ffmpeg=ffmpeg, video_stream=1)
    with Image.open(io.BytesIO(second)) as image:
        assert image.size == (160, 90)


# -- the service ---------------------------------------------------------------

FROM_TOKENS = "custom_components.tapo_cloud.TapoCloudAccount.from_tokens"


async def _setup_with_lens(hass, entry_data, mock_account):
    parent = CameraInfo(device_id=PARENT, name="Driveway", model="DUALLENS", app_server="a")
    lens = CameraInfo(
        device_id=PARENT + "01", name="Driveway PT Lens", model="DUALLENS",
        app_server="a", relay_device_id=PARENT, channel=1,
    )
    mock_account.async_discover = AsyncMock(return_value=[parent, lens])
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="user@example.com")
    entry.add_to_hass(hass)
    with patch(FROM_TOKENS, return_value=mock_account):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_probe_service_returns_and_logs_results(
    hass: HomeAssistant, entry_data, mock_account, caplog
):
    await _setup_with_lens(hass, entry_data, mock_account)
    canned = [
        {"variant": "baseline", "channel": 1, "outcome": "reference picture (lens 0)"},
        {"variant": "app-path", "channel": 1, "outcome": "DIFFERENT picture", "difference": 41.0, "size": "1920x1080"},
    ]
    with (
        patch("custom_components.tapo_cloud.probe.probe_channel", AsyncMock(return_value=canned)) as probe,
        patch("homeassistant.components.ffmpeg.get_ffmpeg_manager") as manager,
    ):
        manager.return_value.binary = "ffmpeg"
        response = await hass.services.async_call(
            DOMAIN, "probe_lenses", {}, blocking=True, return_response=True
        )

    # Only the extra lens is probed, against its parent, on its channel.
    assert probe.await_count == 1
    assert probe.await_args.args[2:4] == (PARENT, 1)
    assert [r["variant"] for r in response["results"]] == ["baseline", "app-path"]
    assert response["results"][1]["camera"] == "Driveway PT Lens"
    assert "Tapo lens probe | Driveway PT Lens | app-path | DIFFERENT picture (difference 41.0, 1920x1080)" in caplog.text
    # No device id in the log or the response.
    assert PARENT not in caplog.text and PARENT not in str(response)


async def test_the_probe_service_refuses_without_a_second_lens(
    hass: HomeAssistant, entry_data, mock_account
):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="user@example.com")
    entry.add_to_hass(hass)
    with patch(FROM_TOKENS, return_value=mock_account):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="No multi-lens camera"):
        await hass.services.async_call(DOMAIN, "probe_lenses", {}, blocking=True, return_response=True)
