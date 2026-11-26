"""Entry setup, discovery into entities, token recovery and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CloudAuthError, CloudTransportError
from custom_components.tapo_cloud.const import CONF_TOKEN, DOMAIN

FROM_TOKENS = "custom_components.tapo_cloud.TapoCloudAccount.from_tokens"


async def _setup(hass: HomeAssistant, entry_data, account) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="user@example.com")
    entry.add_to_hass(hass)
    with patch(FROM_TOKENS, return_value=account):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_creates_a_device_and_entities_per_camera(
    hass: HomeAssistant, entry_data, mock_account
):
    entry = await _setup(hass, entry_data, mock_account)
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), entry.entry_id
    )
    assert {device.name for device in devices} == {"Garden", "Garage"}

    assert hass.states.get("camera.garden") is not None
    assert hass.states.get("camera.garage") is not None
    # The SD-card sensor only exists on the camera that has a card.
    assert hass.states.get("binary_sensor.garage_sd_card_problem") is not None
    assert hass.states.get("binary_sensor.garden_sd_card_problem") is None


async def test_motion_detection_sensor_reflects_the_cloud_reading(
    hass: HomeAssistant, entry_data, mock_account
):
    await _setup(hass, entry_data, mock_account)
    state = hass.states.get("binary_sensor.garden_motion_detection")
    assert state is not None
    assert state.state == "on"


async def test_services_are_registered(hass: HomeAssistant, entry_data, mock_account):
    await _setup(hass, entry_data, mock_account)
    for service in ("pan", "tilt", "move_to_preset", "set_privacy_mode"):
        assert hass.services.has_service(DOMAIN, service)


async def test_pan_service_reaches_the_camera(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(hass, entry_data, mock_account)
    await hass.services.async_call(
        DOMAIN,
        "pan",
        {"entity_id": "camera.garden", "steps": 80},
        blocking=True,
    )
    mock_account.cameras[ptz_camera_info.device_id].async_pan.assert_awaited_once_with(80)


async def test_pan_is_sent_even_when_discovery_found_no_motor(
    hass: HomeAssistant, entry_data, mock_account, fixed_camera_info
):
    """Discovery's capability test misses some PTZ models; let the camera decide."""
    await _setup(hass, entry_data, mock_account)
    assert fixed_camera_info.has_ptz is False
    await hass.services.async_call(
        DOMAIN, "pan", {"entity_id": "camera.garage", "steps": 10}, blocking=True
    )
    mock_account.cameras[fixed_camera_info.device_id].async_pan.assert_awaited_once_with(10)


async def test_a_camera_that_says_unsupported_gets_a_clear_error(
    hass: HomeAssistant, entry_data, mock_account, fixed_camera_info
):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from ontapo import UnsupportedMethod

    await _setup(hass, entry_data, mock_account)
    camera = mock_account.cameras[fixed_camera_info.device_id]
    camera.async_tilt = AsyncMock(side_effect=UnsupportedMethod("-40101"))
    with pytest.raises(HomeAssistantError, match="has no pan/tilt motor"):
        await hass.services.async_call(
            DOMAIN, "tilt", {"entity_id": "camera.garage", "steps": 10}, blocking=True
        )


async def test_an_expired_token_is_refreshed_rather_than_failing_setup(
    hass: HomeAssistant, entry_data, mock_account
):
    """A stale token should recover silently; the user should not be asked again."""
    mock_account.async_discover = AsyncMock(
        side_effect=[CloudAuthError("expired"), []]
    )
    mock_account.async_refresh_token = AsyncMock(
        return_value=type(
            "T",
            (),
            {
                "token": "new-token",
                "refresh_token": "new-refresh",
                "app_server": "aps1",
                "account_base": "https://base",
            },
        )()
    )
    entry = await _setup(hass, entry_data, mock_account)

    assert entry.state is ConfigEntryState.LOADED
    mock_account.async_refresh_token.assert_awaited_once()
    assert entry.data[CONF_TOKEN] == "new-token"


async def test_a_token_that_cannot_be_refreshed_asks_for_reauth(
    hass: HomeAssistant, entry_data, mock_account
):
    mock_account.async_discover = AsyncMock(side_effect=CloudAuthError("expired"))
    mock_account.async_refresh_token = AsyncMock(side_effect=CloudAuthError("dead"))
    entry = await _setup(hass, entry_data, mock_account)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_a_cloud_outage_retries_instead_of_erroring(
    hass: HomeAssistant, entry_data, mock_account
):
    mock_account.async_discover = AsyncMock(side_effect=CloudTransportError("down"))
    entry = await _setup(hass, entry_data, mock_account)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_closes_the_cloud_session(
    hass: HomeAssistant, entry_data, mock_account
):
    entry = await _setup(hass, entry_data, mock_account)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_account.async_close.assert_awaited_once()


async def test_diagnostics_contain_no_credentials(
    hass: HomeAssistant, entry_data, mock_account
):
    from custom_components.tapo_cloud.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass, entry_data, mock_account)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    blob = str(diag)
    assert "test-token" not in blob
    assert "test-refresh-token" not in blob
    assert "user@example.com" not in blob
    assert len(diag["cameras"]) == 2
