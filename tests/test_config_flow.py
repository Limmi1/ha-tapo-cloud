"""The graphical setup flow, including the MFA step and re-authentication."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from ontapo import MFARequired
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo_cloud.api import CloudAuthError, CloudTransportError
from custom_components.tapo_cloud.const import (
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    DOMAIN,
)

CREDS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}

LOGIN = "custom_components.tapo_cloud.config_flow.TapoCloudAccount.login"
ACCOUNT_CLS = "custom_components.tapo_cloud.config_flow.TapoCloudAccount"
SEND_MFA = "custom_components.tapo_cloud.config_flow.async_send_mfa_code"
SUBMIT_MFA = "custom_components.tapo_cloud.config_flow.async_submit_mfa"


def _account(cameras=1):
    account = MagicMock()
    account.async_discover = AsyncMock(return_value=[MagicMock()] * cameras)
    account.async_close = AsyncMock()
    account.tokens = MagicMock(
        token="tok",
        refresh_token="ref",
        terminal_uuid="UUID",
        app_server="aps1",
        account_base="https://base",
    )
    return account


async def test_user_flow_without_mfa_creates_an_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(LOGIN, return_value=_account()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"][CONF_TOKEN] == "tok"
    assert result["data"][CONF_REFRESH_TOKEN] == "ref"


async def test_the_password_is_never_stored_in_the_entry(hass: HomeAssistant):
    """Only tokens are persisted; the password is used once and dropped."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(LOGIN, return_value=_account()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
        await hass.async_block_till_done()

    assert CONF_PASSWORD not in result["data"]
    assert "hunter2" not in str(result["data"])


async def test_mfa_flow_sends_a_code_then_accepts_it(hass: HomeAssistant):
    session = MagicMock()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch(LOGIN, side_effect=MFARequired("proc", [2], session=session)),
        patch(SEND_MFA) as send_mfa,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    # The password is re-sent on the code-send request (the server requires it).
    send_mfa.assert_awaited_once_with(session, CREDS[CONF_PASSWORD])

    with (
        patch(ACCOUNT_CLS, return_value=_account()),
        patch(SUBMIT_MFA) as submit_mfa,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "150-791"}
        )
        await hass.async_block_till_done()

    # The raw code (with its separator) is passed on; digit-stripping lives in
    # async_submit_mfa, which is covered in test_api.
    submit_mfa.assert_awaited_once_with(session, "150-791")
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_wrong_mfa_code_re_prompts(hass: HomeAssistant):
    from custom_components.tapo_cloud.api import CloudAuthError as _AuthErr

    session = MagicMock()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch(LOGIN, side_effect=MFARequired("proc", [2], session=session)),
        patch(SEND_MFA),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
    with patch(SUBMIT_MFA, side_effect=_AuthErr("bad code")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "000000"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}


async def test_a_refused_mfa_send_returns_to_the_form(hass: HomeAssistant):
    """If the server will not send a code, say so rather than hanging on 'sent'."""
    from custom_components.tapo_cloud.api import CloudAuthError as _AuthErr

    session = MagicMock()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch(LOGIN, side_effect=MFARequired("proc", [2], session=session)),
        patch(SEND_MFA, side_effect=_AuthErr("error_code -20615")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "mfa_send_failed"}


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (CloudAuthError("bad"), "invalid_auth"),
        (CloudTransportError("down"), "cannot_connect"),
        (Exception("boom"), "unknown"),
    ],
)
async def test_login_errors_are_shown_on_the_form(
    hass: HomeAssistant, side_effect, expected
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(LOGIN, side_effect=side_effect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_the_same_account_cannot_be_added_twice(hass: HomeAssistant):
    MockConfigEntry(
        domain=DOMAIN, unique_id="user@example.com", data={CONF_EMAIL: "user@example.com"}
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(LOGIN, return_value=_account()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_offers_quality_cadence_and_region(
    hass: HomeAssistant, entry_data
):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="user@example.com")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    keys = {str(key) for key in result["data_schema"].schema}
    assert {"resolution", "snapshot_interval", "relay_host", "live_width", "live_fps"} <= keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "resolution": "VGA",
            "snapshot_interval": 60,
            "relay_host": "euw1-cipc-api.i.tplinkcloud.com",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["resolution"] == "VGA"
