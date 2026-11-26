"""Constants for the Tapo Cloud integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tapo_cloud"

PLATFORMS: Final = ["camera", "binary_sensor", "switch"]

# Config entry keys
CONF_EMAIL: Final = "email"
CONF_TOKEN: Final = "token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_TERMINAL_UUID: Final = "terminal_uuid"
CONF_APP_SERVER: Final = "app_server"
CONF_ACCOUNT_BASE: Final = "account_base"
CONF_RELAY_HOST: Final = "relay_host"

# Options
CONF_RESOLUTION: Final = "resolution"
CONF_SNAPSHOT_INTERVAL: Final = "snapshot_interval"
CONF_STREAM_SECONDS: Final = "stream_seconds"

DEFAULT_RESOLUTION: Final = "HD"
RESOLUTIONS: Final = ["HD", "VGA"]

# A snapshot costs a few seconds of relay streaming plus an ffmpeg decode, so
# it is cached rather than taken on every dashboard poll.
DEFAULT_SNAPSHOT_INTERVAL: Final = 30
DEFAULT_SNAPSHOT_SECONDS: Final = 4.0

UPDATE_INTERVAL: Final = timedelta(minutes=2)

# Availability. The cloud's status passthrough fails intermittently even while
# video works, so a camera is only unavailable after this many failed polls in
# a row (about six minutes at the interval above)...
OFFLINE_AFTER_FAILURES: Final = 3
# ...and video or a snapshot from it within this many seconds proves it online.
MEDIA_PROOF_SECONDS: Final = 300.0

# Live view (options). 0 means "leave as the camera sends it".
CONF_LIVE_WIDTH: Final = "live_width"
CONF_LIVE_FPS: Final = "live_fps"
DEFAULT_LIVE_WIDTH: Final = 1280
DEFAULT_LIVE_FPS: Final = 5

# The relay-negotiation host OnTapo 0.1.0 hardcodes (ap-southeast-1). Other
# regions use the same path under a different prefix; exposed as an option
# because a wrong region is the most likely cause of a relay failure.
DEFAULT_RELAY_HOST: Final = "aps1-cipc-api.i.tplinkcloud.com"
KNOWN_RELAY_HOSTS: Final = [
    "aps1-cipc-api.i.tplinkcloud.com",
    "euw1-cipc-api.i.tplinkcloud.com",
    "use1-cipc-api.i.tplinkcloud.com",
    "aps1-cipc-api.tplinkcloud.com",
]

# Services
SERVICE_PAN: Final = "pan"
SERVICE_TILT: Final = "tilt"
SERVICE_MOVE_TO_PRESET: Final = "move_to_preset"
SERVICE_SET_PRIVACY_MODE: Final = "set_privacy_mode"

ATTR_STEPS: Final = "steps"
ATTR_PRESET: Final = "preset"
ATTR_ENABLED: Final = "enabled"
