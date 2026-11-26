# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.3]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.3
[0.1.2]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.2
[0.1.1]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.1
[0.1.0]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.0
