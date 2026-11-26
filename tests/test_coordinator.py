"""Availability and reconnection behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.tapo_cloud.api import CloudAuthError, CloudTransportError
from custom_components.tapo_cloud.coordinator import TapoCloudCoordinator


def _coordinator(hass, camera, account=None):
    from custom_components.tapo_cloud.api import CameraInfo

    if account is None:
        account = MagicMock()
        account.media_ok_within = MagicMock(return_value=False)  # no recent video
    elif not isinstance(account.media_ok_within.return_value, bool):
        account.media_ok_within = MagicMock(return_value=False)
    account.camera.return_value = camera
    info = CameraInfo(
        device_id="D1", name="Garden", model="C216", app_server="aps1.example.com"
    )
    return TapoCloudCoordinator(hass, account, info)


async def test_a_successful_poll_is_available(hass: HomeAssistant):
    camera = MagicMock()
    camera.async_poll = AsyncMock(return_value={"available": True})
    coordinator = _coordinator(hass, camera)
    await coordinator.async_refresh()
    assert coordinator.available is True


async def test_a_camera_that_answers_but_is_offline_is_unavailable(
    hass: HomeAssistant,
):
    """The cloud replied, but the camera itself is not reachable through it."""
    camera = MagicMock()
    camera.async_poll = AsyncMock(return_value={"available": False})
    coordinator = _coordinator(hass, camera)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.available is False


async def test_an_expired_token_is_refreshed_and_the_poll_retried(
    hass: HomeAssistant,
):
    camera = MagicMock()
    camera.async_poll = AsyncMock(
        side_effect=[CloudAuthError("expired"), {"available": True}]
    )
    account = MagicMock()
    account.async_refresh_token = AsyncMock()
    coordinator = _coordinator(hass, camera, account)

    await coordinator.async_refresh()

    account.async_refresh_token.assert_awaited_once()
    assert coordinator.available is True
    assert camera.async_poll.await_count == 2


async def test_a_refresh_that_does_not_help_fails_the_update(hass: HomeAssistant):
    camera = MagicMock()
    camera.async_poll = AsyncMock(side_effect=CloudAuthError("expired"))
    account = MagicMock()
    account.async_refresh_token = AsyncMock()
    coordinator = _coordinator(hass, camera, account)

    with pytest.raises(UpdateFailed, match="authentication failed"):
        await coordinator._async_update_data()


async def test_a_cloud_outage_is_an_update_failure_not_a_crash(hass: HomeAssistant):
    camera = MagicMock()
    camera.async_poll = AsyncMock(side_effect=CloudTransportError("relay down"))
    coordinator = _coordinator(hass, camera)

    with pytest.raises(UpdateFailed, match="cloud unreachable"):
        await coordinator._async_update_data()


async def test_availability_recovers_on_the_next_successful_poll(
    hass: HomeAssistant,
):
    camera = MagicMock()
    camera.async_poll = AsyncMock(
        side_effect=[CloudTransportError("down"), {"available": True}]
    )
    coordinator = _coordinator(hass, camera)

    await coordinator.async_refresh()
    assert coordinator.available is False

    await coordinator.async_refresh()
    assert coordinator.available is True


# -- riding out a flaky status check (0.1.8) ----------------------------------

from custom_components.tapo_cloud.const import (  # noqa: E402
    MEDIA_PROOF_SECONDS,
    OFFLINE_AFTER_FAILURES,
)

GOOD = {"available": True, "privacy_mode": False, "motion_detection": {"enabled": "on"}}
OFFLINE = {"available": False}


async def test_one_failed_poll_keeps_the_camera_available_with_its_last_state(
    hass: HomeAssistant,
):
    camera = MagicMock()
    camera.async_poll = AsyncMock(side_effect=[GOOD, OFFLINE])
    coordinator = _coordinator(hass, camera)

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.available is True
    assert coordinator.data["privacy_mode"] is False  # carried over, not blanked


async def test_repeated_failures_make_it_unavailable(hass: HomeAssistant):
    camera = MagicMock()
    camera.async_poll = AsyncMock(side_effect=[GOOD] + [OFFLINE] * OFFLINE_AFTER_FAILURES)
    coordinator = _coordinator(hass, camera)

    await coordinator.async_refresh()
    for _ in range(OFFLINE_AFTER_FAILURES - 1):
        await coordinator.async_refresh()
        assert coordinator.available is True
    await coordinator.async_refresh()
    assert coordinator.available is False


async def test_cloud_errors_inside_the_grace_period_do_not_fail_the_update(
    hass: HomeAssistant,
):
    camera = MagicMock()
    camera.async_poll = AsyncMock(
        side_effect=[GOOD] + [CloudTransportError("HTTP 408")] * OFFLINE_AFTER_FAILURES
    )
    coordinator = _coordinator(hass, camera)
    await coordinator.async_refresh()

    for _ in range(OFFLINE_AFTER_FAILURES - 1):
        data = await coordinator._async_update_data()  # no exception
        assert data["privacy_mode"] is False
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_good_poll_resets_the_failure_count(hass: HomeAssistant):
    camera = MagicMock()
    sequence = [GOOD] + [OFFLINE] * (OFFLINE_AFTER_FAILURES - 1) + [GOOD] + [OFFLINE] * (OFFLINE_AFTER_FAILURES - 1)
    camera.async_poll = AsyncMock(side_effect=sequence)
    coordinator = _coordinator(hass, camera)
    for _ in sequence:
        await coordinator.async_refresh()
    assert coordinator.available is True


async def test_recent_video_proves_the_camera_online_whatever_the_poll_says(
    hass: HomeAssistant,
):
    camera = MagicMock()
    camera.async_poll = AsyncMock(return_value=OFFLINE)
    account = MagicMock()
    account.media_ok_within = MagicMock(return_value=True)
    coordinator = _coordinator(hass, camera, account)

    for _ in range(OFFLINE_AFTER_FAILURES + 2):
        await coordinator.async_refresh()
    assert coordinator.available is True
    account.media_ok_within.assert_called_with(coordinator.info.stream_device_id, MEDIA_PROOF_SECONDS)


async def test_a_camera_never_seen_online_is_unavailable(hass: HomeAssistant):
    camera = MagicMock()
    camera.async_poll = AsyncMock(return_value=OFFLINE)
    coordinator = _coordinator(hass, camera)
    await coordinator.async_refresh()
    assert coordinator.available is False
