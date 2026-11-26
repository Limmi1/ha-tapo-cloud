# Architecture

The integration is deliberately split so the cloud transport is separable from
the Home Assistant entity layer. The goal is that the transport could be
contributed to
[HomeAssistant-Tapo-Control](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control)
as an optional *cloud connection mode* alongside its existing local (pytapo)
mode, without dragging any of this repository's entity code along.

## Layers

```
            Home Assistant
  ┌──────────────────────────────────┐
  │ camera.py  binary_sensor.py      │  entity layer
  │ switch.py  services.py           │  — HA-specific, replaceable
  │ config_flow.py  diagnostics.py   │
  └───────────────┬──────────────────┘
                  │  CameraInfo / TapoCloudCamera
  ┌───────────────▼──────────────────┐
  │ coordinator.py                   │  polling + reconnection
  └───────────────┬──────────────────┘
                  │
  ┌───────────────▼──────────────────┐
  │ api.py                           │  ◄── the connection mode
  │ parsers.py   relay_stream.py     │      no HA imports
  └───────────────┬──────────────────┘
                  │
  ┌───────────────▼──────────────────┐
  │ ontapo 0.1.0                     │  cloud protocol (reused, not rewritten)
  └──────────────────────────────────┘
```

## The transport boundary

`api.py`, `parsers.py` and `relay_stream.py` **import no Home Assistant code at
all**, and that is enforced by them being unit-tested without it. Together they
are the whole cloud connection mode:

- **`TapoCloudAccount`** — login (with MFA), token persistence and renewal,
  camera discovery. Yields `CameraInfo` records.
- **`TapoCloudCamera`** — per-camera operations: `iter_live_ts()`,
  `async_snapshot()`, `async_poll()`, pan/tilt/preset, attempted setters.
- **`parsers.py`** — pure functions turning raw device replies into state,
  tolerant of the per-model nesting differences.
- **`relay_stream.py`** — continuous live MPEG-TS (see below).

Errors are translated into a transport-neutral vocabulary at this boundary —
`CloudAuthError`, `CloudTransportError`, `CloudSetterRejected` — so the entity
layer never catches an OnTapo-specific exception. A local connection mode
raising the same four exceptions would slot in behind the same coordinator
unchanged.

To contribute this upstream, the three transport modules move across and the
entity layer is discarded in favour of the host project's own.

## Why `relay_stream.py` exists

OnTapo's public `stream_preview()` records a *fixed number of seconds to a file
path*. Home Assistant needs an open-ended byte stream for as long as a card is
on screen.

Three options were considered:

1. **Re-implement the relay protocol.** Rejected: the explicit brief was to
   reuse OnTapo rather than reverse-engineer the protocol again, and its
   negotiation, framing and error mapping are the valuable part.
2. **Write to a named pipe and have ffmpeg read it.** Rejected: OnTapo opens
   the output path with a blocking `open()`, and opening a FIFO for writing
   blocks until a reader attaches — which would block Home Assistant's event
   loop.
3. **Reuse OnTapo's internals and drive the same loop as an async generator.**
   Chosen.

So `relay_stream.py` calls OnTapo's public `request_relay()` and then its
private framing helpers (`_open_relay`, `_control_frame`, `_read_part`, …). The
private coupling is real, and it is contained:

- it lives in that one module and nowhere else;
- `assert_ontapo_compatible()` checks every reused symbol at setup and raises
  `IncompatibleOnTapo` with an actionable message if one moved;
- the test suite asserts the same list, so a dependency bump fails CI rather
  than a user's dashboard;
- `ontapo` is pinned exactly in `manifest.json`.

If OnTapo later grows a streaming API — an async iterator, or a callback per
chunk — this module collapses to a thin wrapper over it. That would be the
right upstream contribution to OnTapo itself.

## Live video path

```
cloud relay ──TLS──► relay_stream.iter_live_ts()   MPEG-TS chunks
                              │
                              ▼
                     ffmpeg (stdin → stdout)       transcode
                              │
                              ▼
            aiohttp StreamResponse (multipart MJPEG) ──► dashboard
```

The relay session, the ffmpeg process and the HTTP response are torn down
together: whichever end goes away first stops the other two. The MJPEG route is
used rather than Home Assistant's `stream` component because the latter needs a
URL to hand to go2rtc, and the relay offers none — we hold the socket
ourselves.

Snapshots take the simpler route: OnTapo's public file-based `snapshot()`, into
a temporary file that is deleted immediately, cached so a dashboard full of
cards does not open one relay session per card per refresh.

## Phasing

Built in the order the brief asked for, each phase independently abandonable:

1. **Verification** — `scripts/verify_remote_stream.py`, standalone, no Home
   Assistant. If the remote path does not work for a user's region, nothing
   below matters.
2. **Minimal integration** — auth, discovery, live video and snapshots.
3. **Advanced** — PTZ, state sensors, privacy, diagnostics, services.

## Testing

81 tests, none of which touch a network, an account or a camera:

| Area | Approach |
|---|---|
| `parsers.py` | pure unit tests over real-world reply shapes |
| `api.py` | mocked OnTapo session; error-translation table |
| `relay_stream.py` | scripted fake relay (parts, refusals, idle timeouts) + the compatibility guard |
| config flow | `pytest-homeassistant-custom-component`, including the MFA path and that the password is never persisted |
| entities | full HA setup against a fake account; snapshot caching, stale-frame fallback, MJPEG plumbing |
| coordinator | token-refresh-and-retry, availability recovery |
