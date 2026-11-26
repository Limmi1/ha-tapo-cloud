"""The privacy switch, including the cloud's refusal to accept writes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CloudSetterRejected, CloudTransportError
from custom_components.tapo_cloud.const import DOMAIN

FROM_TOKENS = "custom_components.tapo_cloud.TapoCloudAccount.from_tokens"


async def _setup(hass, entry_data, account):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="user@example.com")
    entry.add_to_hass(hass)
    with patch(FROM_TOKENS, return_value=account):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_the_switch_shows_the_cameras_real_state(
    hass: HomeAssistant, entry_data, mock_account
):
    await _setup(hass, entry_data, mock_account)
    state = hass.states.get("switch.garden_privacy_mode")
    assert state is not None
    assert state.state == "off"


async def test_a_gateway_rejection_is_reported_in_plain_language(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    """The user must learn that cloud writes are refused, not see a silent no-op."""
    await _setup(hass, entry_data, mock_account)
    mock_account.cameras[ptz_camera_info.device_id].async_set_privacy_mode = AsyncMock(
        side_effect=CloudSetterRejected("MODEL_SCHEMA_CHECK_FAILED")
    )

    with pytest.raises(HomeAssistantError, match="rejected this change"):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.garden_privacy_mode"},
            blocking=True,
        )


async def test_a_transport_failure_is_reported_separately(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(hass, entry_data, mock_account)
    mock_account.cameras[ptz_camera_info.device_id].async_set_privacy_mode = AsyncMock(
        side_effect=CloudTransportError("relay down")
    )

    with pytest.raises(HomeAssistantError, match="Could not set privacy mode"):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.garden_privacy_mode"},
            blocking=True,
        )


async def test_a_successful_write_is_attempted_with_the_right_value(
    hass: HomeAssistant, entry_data, mock_account, ptz_camera_info
):
    await _setup(hass, entry_data, mock_account)
    camera = mock_account.cameras[ptz_camera_info.device_id]

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.garden_privacy_mode"},
        blocking=True,
    )
    camera.async_set_privacy_mode.assert_awaited_once_with(True)
