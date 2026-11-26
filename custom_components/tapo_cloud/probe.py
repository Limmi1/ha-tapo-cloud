"""Temporary diagnostic: find how the relay selects a multi-lens camera's lens.

The relay accepts a plain ``"channels":[1]`` preview request but still sends
lens 0. The Tapo app's own code builds the relay path itself
(``/relayservice?deviceid=…&type=…&resolution=…&channel=%d``) instead of using
the cloud's signed ``relayUrl``. Rather than guess one variant per release,
this tries each plausible request once, compares each picture with the known
lens-0 picture, and reports which (if any) delivers the other lens.

Run it with the ``tapo_cloud.probe_lenses`` service. Remove this module once a
variant has been chosen.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant

from .api import decode_first_frame
from .relay_stream import iter_relay_ts

_LOGGER = logging.getLogger(__name__)

SECONDS_PER_VARIANT = 3.0
MAX_WAIT_PER_VARIANT = 25.0

# Mean absolute grey-level difference (0-255) between two 64x36 thumbnails.
# Two frames of the same scene a few seconds apart sit well below SAME; two
# different lenses (a wide fixed lens and a pan/tilt lens) sit well above
# DIFFERENT. In between is reported as unclear, with the number.
SAME_BELOW = 12.0
DIFFERENT_ABOVE = 25.0


@dataclass(frozen=True)
class Variant:
    """One way of asking the relay for a channel."""

    name: str
    track_suffix: str
    channels: tuple[int, ...]
    app_path_channel: int | None = None  # None = use the cloud's relayUrl
    decode_stream: int = 0  # which video stream in the TS to decode


def variants_for(channel: int) -> list[Variant]:
    """The request variants to try for ``channel``, baseline first."""
    n = int(channel)
    return [
        Variant("baseline", "", (0,)),
        Variant("frame-only", f"-ch{n}", (n,)),
        Variant("app-path-ch0", "", (0,), app_path_channel=0),
        Variant("app-path", f"-ch{n}", (n,), app_path_channel=n),
        Variant("app-path-frame0", f"-ch{n}", (0,), app_path_channel=n),
        Variant("both-channels", "", (0, n), decode_stream=n),
    ]


def app_path_url(
    relay_url: str, device_id: str, resolution: str, channel: int
) -> str:
    """The relay URL built the way the Tapo app formats it, on the same host."""
    host = urlparse(relay_url).netloc
    return (
        f"https://{host}/relayservice?deviceid={device_id}"
        f"&type=video&resolution={resolution}&channel={int(channel)}"
    )


async def collect_ts(
    stream: AsyncIterator[bytes],
    seconds: float = SECONDS_PER_VARIANT,
    max_wait: float = MAX_WAIT_PER_VARIANT,
) -> bytes:
    """Gather about ``seconds`` of video from ``stream``, then close it."""
    loop = asyncio.get_running_loop()
    chunks: list[bytes] = []

    async def gather() -> None:
        deadline: float | None = None
        async with contextlib.aclosing(stream) as live:
            async for chunk in live:
                chunks.append(chunk)
                if deadline is None:
                    deadline = loop.time() + seconds
                if loop.time() >= deadline:
                    return

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(gather(), timeout=max_wait)
    return b"".join(chunks)


_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?(\d{2,5})x(\d{2,5})")


async def video_streams(video: bytes, ffmpeg: str) -> list[str]:
    """Describe each video stream in an MPEG-TS buffer, e.g. ``h264 2304x1296``."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-f", "mpegts", "-i", "pipe:0",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate(video)
    return parse_video_streams(err.decode(errors="replace"))


def parse_video_streams(ffmpeg_stderr: str) -> list[str]:
    return [
        f"{codec} {width}x{height}"
        for codec, width, height in _VIDEO_STREAM.findall(ffmpeg_stderr)
    ]


def compare_images(reference: bytes, image: bytes) -> tuple[float, str, str]:
    """``(difference, verdict, size)`` of ``image`` against ``reference``.

    Runs in an executor: Pillow decoding is CPU work.
    """
    from PIL import Image, ImageChops, ImageStat

    with Image.open(io.BytesIO(image)) as candidate:
        size = f"{candidate.width}x{candidate.height}"
        small = candidate.convert("L").resize((64, 36))
    with Image.open(io.BytesIO(reference)) as ref:
        ref_small = ref.convert("L").resize((64, 36))
    difference = ImageStat.Stat(ImageChops.difference(small, ref_small)).mean[0]
    if difference < SAME_BELOW:
        verdict = "same picture as lens 0"
    elif difference > DIFFERENT_ABOVE:
        verdict = "DIFFERENT picture"
    else:
        verdict = "unclear"
    return round(difference, 1), verdict, size


async def probe_channel(
    hass: HomeAssistant,
    session: object,
    parent_id: str,
    channel: int,
    *,
    ffmpeg: str,
    resolution: str = "HD",
    save_dir: Path | None = None,
    stream_factory: Callable[..., AsyncIterator[bytes]] = iter_relay_ts,
) -> list[dict[str, Any]]:
    """Try each variant for ``channel`` in turn; return one result per variant."""
    results: list[dict[str, Any]] = []
    reference: bytes | None = None

    for variant in variants_for(channel):
        result: dict[str, Any] = {"variant": variant.name, "channel": channel}
        relay_url = None
        if variant.app_path_channel is not None:
            app_channel = variant.app_path_channel

            def relay_url(relay, _c=app_channel):  # noqa: ANN001, ANN202
                return app_path_url(relay.relay_url, parent_id, resolution, _c)

        try:
            video = await collect_ts(
                stream_factory(
                    session,
                    parent_id,
                    track_id=f"preview-{parent_id}{variant.track_suffix}",
                    channels=list(variant.channels),
                    resolution=resolution,
                    relay_url=relay_url,
                )
            )
        except Exception as err:  # noqa: BLE001 - every outcome is a result
            result["outcome"] = f"refused or failed: {err}"
            results.append(result)
            continue
        if not video:
            result["outcome"] = "no video received"
            results.append(result)
            continue

        result["video_streams"] = await video_streams(video, ffmpeg)
        try:
            image = await decode_first_frame(
                video, ffmpeg=ffmpeg, video_stream=variant.decode_stream
            )
        except RuntimeError as err:
            result["outcome"] = f"video received, no frame: {err}"
            results.append(result)
            continue

        if variant.name == "baseline":
            reference = image
            result["outcome"] = "reference picture (lens 0)"
        elif reference is None:
            result["outcome"] = "picture received, but no baseline to compare with"
        else:
            difference, verdict, size = await hass.async_add_executor_job(
                compare_images, reference, image
            )
            result.update(outcome=verdict, difference=difference, size=size)

        if save_dir is not None:
            path = save_dir / f"ch{channel}_{variant.name}.jpg"
            await hass.async_add_executor_job(_save, path, image)
            result["saved"] = f"/local/tapo_probe/{path.name}"
        results.append(result)

    return results


def _save(path: Path, image: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
