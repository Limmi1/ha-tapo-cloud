# Tapo Cloud

Connect TP-Link Tapo cameras to Home Assistant through your Tapo **cloud
account**, over TP-Link's own media relay — including cameras on entirely
separate routers and internet connections.

No VPN, no port forwarding, no gateway hardware, no camera-local password.

- Login with MFA, automatic camera discovery
- Live video and snapshots in dashboards
- Pan, tilt and presets
- Motion detection and privacy mode state (read only — the cloud rejects
  setting writes)

**Before installing, read `docs/VERIFICATION.md`.** This builds on an alpha
library talking to undocumented TP-Link endpoints: it can break without notice
and may conflict with TP-Link's terms of service. Run
`scripts/verify_remote_stream.py` first to confirm the remote path works from
your region.

Requires ffmpeg (already present on Home Assistant OS and Container).
