# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/Limmi1/ha-tapo-cloud/releases/tag/v0.1.0
