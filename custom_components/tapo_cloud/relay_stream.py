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
import logging
from collections.abc import AsyncIterator
from dataclasses import replace

from ontapo import RelayError
from ontapo.relay import StreamType, request_relay

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


def with_channel(relay_url: str, channel: int) -> str:
    """Add ``channel=<n>`` to a relay URL's query unless it already has one.

    The Tapo app requests ``/relayservice?deviceid=…&type=…&resolution=…&channel=%d``
    and picks a multi-lens camera's lens by channel on the parent device. The
    cloud's ``relayUrl`` carries an opaque routing query; this keeps that query
    as-is and only appends the channel when it is absent.
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(relay_url)
    if "channel" in parse_qs(parsed.query, keep_blank_values=True):
        return relay_url
    separator = "&" if parsed.query else ("" if relay_url.endswith("?") else "?")
    return f"{relay_url}{separator}channel={int(channel)}"


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
    from ontapo import stream as _s

    assert_ontapo_compatible()
    gap = _s._IDLE_GAP if idle_timeout is None else idle_timeout

    channel = int(channel)
    # One track per (device, channel), so two lenses of one camera never share
    # a relay session.
    track_id = f"ha-preview-{device_id}-ch{channel}"
    relay = await request_relay(session, device_id, StreamType.PREVIEW, track_id)
    relay = replace(relay, relay_url=with_channel(relay.relay_url, channel))
    terminal_uuid = session.terminal_uuid  # type: ignore[attr-defined]

    reader, writer, host, req_path = await _s._open_relay(relay, terminal_uuid, track_id)
    try:
        writer.write(_s._relay_head(relay, terminal_uuid, track_id, req_path, host).encode())
        preview_req = (
            '{"type":"request","seq":1,"params":{"preview":'
            f'{{"audio":["default"],"channels":[{channel}],"resolutions":["{resolution}"]}},'
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
