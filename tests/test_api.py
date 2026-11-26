"""The cloud adapter: error translation, discovery, token handling, secrecy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ontapo import AuthError, CloudError, RelayError, Unauthorized, UnsupportedMethod

from custom_components.tapo_cloud.api import (
    CameraInfo,
    CloudAuthError,
    CloudSetterRejected,
    CloudTransportError,
    SessionTokens,
    TapoCloudAccount,
    _map_error,
    redact,
)


def test_redact_hides_secrets_but_keeps_shape():
    out = redact({"token": "abc", "refresh_token": "def", "email": "a@b.c"})
    assert out["token"] == "**REDACTED**"
    assert out["refresh_token"] == "**REDACTED**"
    assert out["email"] == "a@b.c"


def test_redact_leaves_empty_secrets_alone():
    assert redact({"token": ""})["token"] == ""


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (AuthError("bad"), CloudAuthError),
        (Unauthorized("expired"), CloudAuthError),
        (RelayError("relay down"), CloudTransportError),
        (CloudError("gateway exploded"), CloudTransportError),
    ],
)
def test_error_translation(raised, expected):
    assert isinstance(_map_error(raised), expected)


def test_schema_rejection_is_distinguished_from_a_transport_fault():
    """A refused setter must not look like an outage, or it would be retried."""
    err = _map_error(CloudError("http 400 MODEL_SCHEMA_CHECK_FAILED"))
    assert isinstance(err, CloudSetterRejected)


def _session_with(cameras, summary=None, fail_summary=False):
    session = MagicMock()
    session.cameras = AsyncMock(return_value=cameras)
    device = MagicMock()
    if fail_summary:
        device.summary = AsyncMock(side_effect=CloudError("nope"))
    else:
        device.summary = AsyncMock(return_value=summary)
    session.device.return_value = device
    return session


async def test_discovery_enriches_cameras_with_capabilities():
    raw = SimpleNamespace(
        device_id="D1", name="Garden", model="C216", app_server="aps1.example.com"
    )
    summary = SimpleNamespace(
        name="Garden",
        model="C216",
        firmware="1.3.1",
        has_ptz=True,
        has_sd_card=False,
        features=frozenset({"ptz"}),
    )
    account = TapoCloudAccount(_session_with([raw], summary))
    cameras = await account.async_discover()
    assert cameras == [
        CameraInfo(
            device_id="D1",
            name="Garden",
            model="C216",
            app_server="aps1.example.com",
            firmware="1.3.1",
            has_ptz=True,
            has_sd_card=False,
            features=frozenset({"ptz"}),
        )
    ]


async def test_a_camera_that_fails_its_capability_probe_is_still_listed():
    """It should show as unavailable, not disappear from the dashboard."""
    raw = SimpleNamespace(
        device_id="D2", name="Garage", model="TC65", app_server="aps1.example.com"
    )
    account = TapoCloudAccount(_session_with([raw], fail_summary=True))
    cameras = await account.async_discover()
    assert len(cameras) == 1
    assert cameras[0].device_id == "D2"
    assert cameras[0].has_ptz is False


async def test_discovery_auth_failure_is_translated():
    session = MagicMock()
    session.cameras = AsyncMock(side_effect=Unauthorized("expired"))
    with pytest.raises(CloudAuthError):
        await TapoCloudAccount(session).async_discover()


def test_tokens_round_trip_through_the_persistable_form():
    tokens = SessionTokens(
        token="t",
        refresh_token="r",
        terminal_uuid="U",
        app_server="aps1",
        account_base="https://base",
    )
    assert tokens.as_dict()["token"] == "t"
    assert set(tokens.as_dict()) == {
        "token",
        "refresh_token",
        "terminal_uuid",
        "app_server",
        "account_base",
    }


async def test_relay_host_override_is_applied_and_reversible(monkeypatch):
    from ontapo import relay as ontapo_relay

    original = ontapo_relay.CIPC_HOST
    try:
        TapoCloudAccount(MagicMock(), relay_host="euw1-cipc-api.i.tplinkcloud.com")
        assert ontapo_relay.CIPC_HOST == "euw1-cipc-api.i.tplinkcloud.com"
    finally:
        ontapo_relay.CIPC_HOST = original


async def test_poll_marks_a_silent_camera_unavailable():
    session = MagicMock()
    device = MagicMock()
    device.device_info = AsyncMock(side_effect=CloudError("timeout"))
    session.device.return_value = device
    account = TapoCloudAccount(session)
    info = CameraInfo(device_id="D", name="n", model="m", app_server="a")
    state = await account.camera(info).async_poll()
    assert state["available"] is False


async def test_poll_records_unsupported_features_as_none_not_a_failure():
    session = MagicMock()
    device = MagicMock()
    device.device_info = AsyncMock(return_value={"device_model": "TC65"})
    device.get_privacy_mode = AsyncMock(side_effect=UnsupportedMethod("-40101"))
    device.get_motion_detection = AsyncMock(return_value={"enabled": "on"})
    session.device.return_value = device
    account = TapoCloudAccount(session)
    info = CameraInfo(device_id="D", name="n", model="m", app_server="a")
    state = await account.camera(info).async_poll()
    assert state["available"] is True
    assert state["privacy_mode"] is None
    assert state["motion_detection"] == {"enabled": "on"}


async def test_setting_privacy_surfaces_the_gateway_rejection():
    session = MagicMock()
    device = MagicMock()
    device.set_privacy_mode = AsyncMock(
        side_effect=CloudError("http 400 MODEL_SCHEMA_CHECK_FAILED")
    )
    session.device.return_value = device
    account = TapoCloudAccount(session)
    info = CameraInfo(device_id="D", name="n", model="m", app_server="a")
    with pytest.raises(CloudSetterRejected):
        await account.camera(info).async_set_privacy_mode(True)


async def test_pan_and_tilt_reach_the_device():
    session = MagicMock()
    device = MagicMock()
    device.pan = AsyncMock(return_value={})
    device.tilt = AsyncMock(return_value={})
    session.device.return_value = device
    info = CameraInfo(device_id="D", name="n", model="m", app_server="a")
    camera = TapoCloudAccount(session).camera(info)
    await camera.async_pan(80)
    await camera.async_tilt(-40)
    device.pan.assert_awaited_once_with(80)
    device.tilt.assert_awaited_once_with(-40)


async def test_submit_mfa_strips_separators_from_the_code():
    """A grouped code like 150-791 must be reduced to digits before submission."""
    from custom_components.tapo_cloud.api import async_submit_mfa

    session = MagicMock()
    session.submit_mfa = AsyncMock()
    await async_submit_mfa(session, "150-791")
    session.submit_mfa.assert_awaited_once_with("150791", 2)


async def test_submit_mfa_rejects_a_code_with_no_digits():
    from custom_components.tapo_cloud.api import CloudAuthError, async_submit_mfa

    with pytest.raises(CloudAuthError, match="no digits"):
        await async_submit_mfa(MagicMock(), "----")


async def test_send_mfa_code_resends_the_password_and_checks_the_body():
    """The password goes out as cloudPassword; a 200 error_code body is a failure."""
    from custom_components.tapo_cloud.api import (
        CloudAuthError,
        async_send_mfa_code,
    )

    session = MagicMock()
    session.email = "user@example.com"
    session._mfa_process_id = "proc"
    session.terminal_uuid = "UUID"
    session.account_base = "https://base"
    session._signed_headers = MagicMock(return_value={})

    captured: dict[str, bytes] = {}

    async def fake_post(url, content, headers):
        captured["body"] = content
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"error_code": -20615, "msg": "Password format error"})
        return resp

    session._client = MagicMock()
    session._client.post = AsyncMock(side_effect=fake_post)

    with pytest.raises(CloudAuthError, match="-20615"):
        await async_send_mfa_code(session, "hunter2")

    import json

    body = json.loads(captured["body"])
    assert body["cloudPassword"] == "hunter2"
    assert body["cloudUserName"] == "user@example.com"


async def test_send_mfa_code_succeeds_on_a_clean_body():
    from custom_components.tapo_cloud.api import async_send_mfa_code

    session = MagicMock()
    session.email = "user@example.com"
    session._mfa_process_id = "proc"
    session.terminal_uuid = "UUID"
    session.account_base = "https://base"
    session._signed_headers = MagicMock(return_value={})
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"error_code": 0})
    session._client = MagicMock()
    session._client.post = AsyncMock(return_value=resp)

    await async_send_mfa_code(session, "pw", mfa_type=2)  # no raise
