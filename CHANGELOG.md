# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.8] - 2026-11-26

### Fixed

- **Cameras flapping between available and unavailable.** A single failed
  status check from the cloud (often HTTP 408) marked a camera unavailable.
  Home Assistant then revoked its access token, so open live views went
  blank even though the video relay was still working. A camera now goes
  unavailable only after three failed checks in a row, keeps its last good
  state meanwhile, and stays available whenever video or a snapshot from it
  arrived in the last five minutes. Every camera and lens follows the same
  rule.
- **Motor commands failing with HTTP 408.** Pan, tilt and presets now retry
  a cloud timeout twice (after 0.5 s and 1.5 s) and log the cloud's message
  each time.
- **Live view could produce no frames with newer ffmpeg.** The live-view
  ffmpeg command used `-fflags nobuffer`, which stops ffmpeg 8 decoding
  piped video at all, and didn't request full-range output, which ffmpeg 8's
  MJPEG encoder requires. Both are fixed, and tested against real ffmpeg.
- Cloud errors are now classified by HTTP status code. A message that merely
  contained "400" could previously be mistaken for a refused setting change.

### Added

- Live view sends the last frame immediately, then switches to live frames.
- Options for live view width (default 1280 px, never upscaling) and frame
  rate (default 5 fps). The camera's native stream is about 3.5 MB/s per
  viewer; the defaults cut that to roughly a tenth. Set either to 0 to keep
  the camera's own value.

## [0.1.7] - 2026-11-26

### Changed

- **Camera tiles show the last picture straight away.** Before, when a
  picture was due for a refresh, Home Assistant waited the several seconds
  it takes to pull a new one, so cards sat blank or loading. Now the last
  frame is returned at once and a newer one is fetched in the background
  (at most one at a time per camera), so the next refresh shows it. Only the
  first picture after a restart is waited for. Refresh timing still follows
  the "Minimum seconds between snapshots" option.

## [0.1.6] - 2026-11-26

### Fixed

- **Pan and tilt were refused on cameras that do have a motor.** The services
  checked a capability flag from discovery first, and that flag relies on the
  camera listing a `ptz` component. Some models, including at least one
  dual-lens camera, report their motor differently. The services now send
  the move and let the camera decide. If the camera answers "unsupported",
  you get a clear "has no pan/tilt motor" error. Pan and tilt on a lens of a
  multi-lens camera go to the parent device, which owns the motor.

## [0.1.5] - 2026-11-26

### Fixed

- **The second lens of a dual-lens camera showed the first lens's picture.**
  The 0.1.4 lens probe showed that the relay sends every lens in a single
  stream, as separate video streams in lens order, and that the relay ignores
  a request for one lens alone. Snapshots and live view now decode the lens's
  own video stream: stream 0 for an ordinary camera or the first lens, stream
  1 for the second lens, and so on.

### Removed

- The temporary `tapo_cloud.probe_lenses` diagnostic service from 0.1.4. It
  also showed that a relay address built the way the Tapo app formats it is
  refused (HTTP 401), so that approach is ruled out.

## [0.1.4] - 2026-11-26

### Added

- Temporary diagnostic service `tapo_cloud.probe_lenses` for multi-lens
  cameras. For each extra lens it tries every candidate way of asking the
  relay for that lens, one at a time, compares each picture with lens 0, and
  reports which request (if any) delivers the other lens. Results are returned
  by the service and logged, and the pictures are saved under
  `/config/www/tapo_probe/`. It will be removed once the right request is
  known.

### Changed

- The relay session is now a lower-level function with the track name,
  channel list and relay path as parameters. Normal streaming sends exactly
  the same request as 0.1.3.
- Stopping a live stream early closes its relay connection immediately.

## [0.1.3] - 2026-11-26

### Fixed

- **No camera pictures at all in 0.1.2.** 0.1.2 added `channel=` to the
  relay's signed address and renamed the relay track, and TP-Link's relay
  rejected every stream with HTTP 401, including ordinary single-lens
  cameras. Channel 0 now sends exactly the request that worked in 0.1.1. A
  lens other than the first differs only in its track name and in the
  `"channels":[n]` control message that selects it.

### Changed

- Snapshot failures now log a warning the first time a camera starts failing,
  and a note when it recovers. Before, they were only visible with debug
  logging on.

## [0.1.2] - 2026-11-26

### Fixed

- **Multi-lens cameras.** The cloud lists a dual-lens camera as a parent device
  plus one device per lens (the parent's id with `00`, `01`, … appended), but
  refuses both cloud calls and relay streams addressed to a lens. Lenses are
  now recognised during discovery and streamed the way the Tapo app does it:
  the **parent** device on the lens's **channel**. Each lens gets its own
  picture, and two lenses no longer share a relay session.
- A lens's availability, privacy and motion now come from its parent camera's
  poll, and nothing is ever sent to a lens id. The parent is polled once, not
  once per lens.
- A relay that keeps refusing the token no longer renews it on every snapshot
  of every card. Renewals for snapshots and live view are limited to one every
  five minutes across the account, and renewals by the background poll count
  towards that limit.
- Snapshots no longer do blocking file I/O on Home Assistant's event loop.
  Video now goes to ffmpeg and the JPEG comes back over pipes, with no
  temporary files.
- A token refusal during a snapshot now shows the last good frame instead of a
  broken image.

### Removed

- The 0.1.1 workaround that marked lenses available when the cloud said they
  didn't belong to the account. The lens mapping replaces it.

## [0.1.1] - 2026-11-26

### Fixed

- Snapshots never recovered when the cloud relay rejected the session token
  while other cloud calls still accepted it. A relay refusal now renews the
  token once and retries the snapshot.

### Changed

- Experimental: individual lenses of multi-lens cameras are kept available.
  The cloud lists each lens as its own device but refuses settings reads for
  it ("Thing doesn't belong to user's account"), which marked every lens
  unavailable so its snapshot was never tried. Lenses now stay available so
  the relay can be tried; their privacy and motion state reads as unknown.

## [0.1.0] - 2026-11-26

### Added

- Sign-in with a Tapo cloud account through a graphical config flow, including
  two-factor verification by Tapo app or email.
- Automatic discovery of every camera on the account.
- Live video on dashboards, transcoded from the cloud relay's MPEG-TS to MJPEG.
- Cached snapshots, with the last good frame served if the relay hiccups.
- Pan, tilt and preset services for cameras with a motor.
- Read-only motion detection, privacy mode and SD-card health sensors.
- Automatic token renewal, reconnection and re-authentication prompts.
- Diagnostics with credentials, email and device ids redacted.
- A standalone `scripts/verify_remote_stream.py` to check the remote path works
  before installing.
- Integration icon for Home Assistant.

### Fixed (relative to OnTapo 0.1.0)

- Two-factor codes never arrived: the code request must re-send the password,
  and the server reports refusals as HTTP 200, which looked like success.
- Codes typed with a separator, such as `150-791`, were rejected.

[0.1.8]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.8
[0.1.7]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.7
[0.1.6]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.6
[0.1.5]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.5
[0.1.4]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.4
[0.1.3]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.3
[0.1.2]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.2
[0.1.1]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.1
[0.1.0]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.0
