"""Shared fixtures. Nothing here touches a real account, camera or network."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest_plugins = ["pytest_homeassistant_custom_component"]

from custom_components.tapo_cloud.api import CameraInfo  # noqa: E402
from custom_components.tapo_cloud.const import (  # noqa: E402
    CONF_ACCOUNT_BASE,
    CONF_APP_SERVER,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_TERMINAL_UUID,
    CONF_TOKEN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ during tests."""
    return enable_custom_integrations


@pytest.fixture
def entry_data() -> dict[str, str]:
    return {
        CONF_EMAIL: "user@example.com",
        CONF_TOKEN: "test-token",
        CONF_REFRESH_TOKEN: "test-refresh-token",
        CONF_TERMINAL_UUID: "ABCDEF0123456789",
        CONF_APP_SERVER: "aps1-app-server.iot.i.tplinkcloud.com",
        CONF_ACCOUNT_BASE: "https://n-wap.i.tplinkcloud.com",
    }


@pytest.fixture
def ptz_camera_info() -> CameraInfo:
    return CameraInfo(
        device_id="DEVICE-PTZ-1",
        name="Garden",
        model="C216",
        app_server="aps1-app-server.iot.i.tplinkcloud.com",
        firmware="1.3.1",
        has_ptz=True,
        has_sd_card=False,
        features=frozenset({"ptz", "whiteLamp"}),
    )


@pytest.fixture
def fixed_camera_info() -> CameraInfo:
    return CameraInfo(
        device_id="DEVICE-FIXED-1",
        name="Garage",
        model="TC65",
        app_server="aps1-app-server.iot.i.tplinkcloud.com",
        firmware="1.9.2",
        has_ptz=False,
        has_sd_card=True,
        features=frozenset({"sdCard"}),
    )


@pytest.fixture
def mock_account(ptz_camera_info, fixed_camera_info):
    """A TapoCloudAccount stand-in that answers without any network."""
    account = MagicMock()
    account.async_discover = AsyncMock(
        return_value=[ptz_camera_info, fixed_camera_info]
    )
    account.async_close = AsyncMock()
    account.async_refresh_token = AsyncMock()

    cameras: dict[str, MagicMock] = {}

    def make_camera(info):
        camera = cameras.get(info.device_id)
        if camera is not None:
            return camera
        camera = MagicMock()
        camera.info = info
        camera.device_id = info.device_id
        camera.async_poll = AsyncMock(
            return_value={
                "available": True,
                "device_info": {"device_model": info.model},
                "privacy_mode": False,
                "motion_detection": {"motion_det": {"enabled": "on"}},
                "sd_card": None,
            }
        )
        camera.async_snapshot = AsyncMock()
        camera.async_pan = AsyncMock()
        camera.async_tilt = AsyncMock()
        camera.async_move_to_preset = AsyncMock()
        camera.async_set_privacy_mode = AsyncMock()
        cameras[info.device_id] = camera
        return camera

    account.camera.side_effect = make_camera
    account.cameras = cameras
    return account
