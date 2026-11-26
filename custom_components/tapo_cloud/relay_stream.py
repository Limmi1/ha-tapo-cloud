"""Continuous live MPEG-TS from the Tapo cloud relay.

This is the **only** module in the integration that touches OnTapo's private
helpers, and it is deliberately quarantined here.

Why it exists: OnTapo's public :func:`ontapo.stream.stream_preview` records a
*fixed number of seconds* to a *file path*. Home Assistant needs an open-ended
byte stream it can pipe into ffmpeg for as long as a dashboard card is on
screen. Rather than re-reverse-engineering the relay protocol (OnTapo's own
negotiation, framing and error handling are reused verbatim), we reuse its
internals and drive the same loop as an async generator.

The coupling is checked at runtime by :func:`assert_ontapo_compatible`, which is
also asserted by the test suite, so an incompatible OnTapo release fails loudly
with an actionable message instead of streaming garbage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

from ontapo import RelayError
from ontapo.relay import RelayParams, StreamType, request_relay

_LOGGER = logging.getLogger(__name__)

# Private helpers reused from OnTapo. Keep this list in sync with
# assert_ontapo_compatible() below.
_REQUIRED_INTERNALS = (
    "_open_relay",
    "_relay_head",
    "_control_frame",
    "_read_status",
    "_read_headers",
    "_read_part",
    "_control_error",
    "_stream_refused",
    "_IDLE_GAP",
)


class IncompatibleOnTapo(RuntimeError):
    """The installed OnTapo release does not expose the internals we reuse."""


def assert_ontapo_compatible() -> None:
    """Raise :class:`IncompatibleOnTapo` if OnTapo's relay internals moved.

    Called once at setup so a bad upgrade surfaces as a clear repair message
    rather than a broken camera.
    """
    from ontapo import stream as _stream

    missing = [name for name in _REQUIRED_INTERNALS if not hasattr(_stream, name)]
    if missing:
        import ontapo

        raise IncompatibleOnTapo(
            f"ontapo {ontapo.__version__} is missing relay internals this "
            f"integration reuses for continuous live streaming: "
            f"{', '.join(missing)}. Pin ontapo==0.1.0 or update Tapo Cloud."
        )


async def iter_live_ts(
    session: object,
    device_id: str,
    *,
    channel: int = 0,
    resolution: str = "HD",
    idle_timeout: float | None = None,
) -> AsyncIterator[bytes]:
    """Yield MPEG-TS chunks of a camera's live view until the consumer stops.

    Runs entirely over the Tapo cloud relay: no LAN access to the camera, no
    port forwarding, no VPN, no camera-local password. The caller's account
    token authorises the media session.

    Args:
        session: an :class:`ontapo.OnTapo` logged-in session.
        device_id: the cloud device id the relay is negotiated for. For a lens
            of a multi-lens camera this is the **parent** device's id.
        channel: which video channel to stream. ``0`` for a single-lens camera;
            for a multi-lens camera, the lens number.
        resolution: relay stream profile, ``"HD"`` or ``"VGA"``.
        idle_timeout: seconds of silence that end the stream. Defaults to
            OnTapo's own ``_IDLE_GAP``.

    Raises:
        RelayError: negotiation failed, the relay refused, or the stream ended
            before producing any video.
    """
    channel = int(channel)
    # Channel 0 must be byte-for-byte the request OnTapo's own live view makes:
    # same track id, relay URL untouched. Live testing showed that altering
    # either (0.1.2 appended ``channel=`` to the signed relay URL and renamed
    # the track) makes the relay answer 401. Another lens differs only in its
    # track id, so two lenses never share a session, and in the
    # ``"channels":[n]`` control frame, which is what selects the lens.
    track_id = f"preview-{device_id}" if channel == 0 else f"preview-{device_id}-ch{channel}"
    # aclosing: when the consumer stops early (a dashboard card closes), the
    # inner session must close its relay socket now, not at garbage collection.
    async with contextlib.aclosing(
        iter_relay_ts(
            session,
            device_id,
            track_id=track_id,
            channels=[channel],
            resolution=resolution,
            idle_timeout=idle_timeout,
        )
    ) as stream:
        async for chunk in stream:
            yield chunk


async def iter_relay_ts(
    session: object,
    device_id: str,
    *,
    track_id: str,
    channels: list[int],
    resolution: str = "HD",
    relay_url: Callable[[RelayParams], str] | None = None,
    idle_timeout: float | None = None,
) -> AsyncIterator[bytes]:
    """The relay session itself, with every request detail explicit.

    :func:`iter_live_ts` is the normal entry point. This exists so the lens
    probe can try request variants (a different track name, channel list or
    relay path) without duplicating the protocol. ``relay_url``, if given,
    maps the cloud's relay parameters to the URL to connect to instead of the
    cloud's own ``relayUrl``; the ``X-token`` authorisation is unchanged.
    """
    from ontapo import stream as _s

    assert_ontapo_compatible()
    gap = _s._IDLE_GAP if idle_timeout is None else idle_timeout

    relay = await request_relay(session, device_id, StreamType.PREVIEW, track_id)
    if relay_url is not None:
        relay = replace(relay, relay_url=relay_url(relay))
    terminal_uuid = session.terminal_uuid  # type: ignore[attr-defined]

    reader, writer, host, req_path = await _s._open_relay(relay, terminal_uuid, track_id)
    try:
        writer.write(_s._relay_head(relay, terminal_uuid, track_id, req_path, host).encode())
        preview_req = (
            '{"type":"request","seq":1,"params":{"preview":'
            f'{{"audio":["default"],"channels":{json.dumps(channels, separators=(",", ":"))},"resolutions":["{resolution}"]}},'
            '"method":"get"}}'
        )
        writer.write(_s._control_frame(preview_req))
        await writer.drain()

        status = await _s._read_status(reader)
        headers = await _s._read_headers(reader)
        if status != 200:
            raise RelayError(
                f"relay preview HTTP {status} ({headers.get('x-request-type')})"
            )

        produced = 0
        while True:
            try:
                content_type, payload = await asyncio.wait_for(
                    _s._read_part(reader), timeout=gap
                )
            except (TimeoutError, asyncio.IncompleteReadError) as err:
                if produced:
                    _LOGGER.debug(
                        "live stream for %s ended after %d bytes", device_id, produced
                    )
                    return
                raise RelayError("relay preview ended before any data") from err
            except asyncio.LimitOverrunError as err:
                raise RelayError(f"relay preview: oversized part ({err})") from err

            if "video/mp2t" in content_type:
                produced += len(payload)
                yield payload
            elif "application/json" in content_type:
                code = _s._control_error(payload)
                if code:
                    raise _s._stream_refused(code)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
