"""The cloud adapter: error translation, discovery, token handling, secrecy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ontapo import AuthError, CloudError, RelayError, Unauthorized, UnsupportedMethod

from custom_components.tapo_cloud.api import (
    LENS_POLL_SHARE_SECONDS,
    TOKEN_REFRESH_MIN_INTERVAL,
    CameraInfo,
    CloudAuthError,
    CloudSetterRejected,
    CloudTransportError,
    SessionTokens,
    TapoCloudAccount,
    _map_error,
    decode_first_frame,
    map_lenses,
    redact,
)

API = "custom_components.tapo_cloud.api"


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



# -- multi-lens cameras ------------------------------------------------------

PARENT = "FAKEDEVICE0123456789"


def test_map_lenses_finds_the_lenses_of_a_dual_lens_camera():
    lenses = map_lenses([PARENT, PARENT + "00", PARENT + "01", "OTHERCAM"])
    assert lenses == {PARENT + "00": (PARENT, 0), PARENT + "01": (PARENT, 1)}


@pytest.mark.parametrize(
    "ids",
    [
        ["CAMA", "CAMB"],  # unrelated cameras
        [PARENT + "00"],  # lens-like id with no parent listed
        [PARENT, PARENT + "0A"],  # suffix is not two digits
        [PARENT, PARENT + "001"],  # three characters, not two
        [PARENT, PARENT + "\u0661\u0662"],  # non-ASCII digits
    ],
)
def test_map_lenses_leaves_ordinary_cameras_alone(ids):
    assert map_lenses(ids) == {}


def _thing(device_id, name, model="DUALLENS"):
    return SimpleNamespace(device_id=device_id, name=name, model=model, app_server="aps1")


async def test_discovery_describes_lenses_from_their_parent_without_probing_them():
    session = MagicMock()
    session.cameras = AsyncMock(
        return_value=[
            _thing(PARENT, "Driveway"),
            _thing(PARENT + "00", "Fixed Lens"),
            _thing(PARENT + "01", "PT Lens"),
        ]
    )
    summary = SimpleNamespace(
        name="Driveway", model="DUALLENS", firmware="1.0.0",
        has_ptz=True, has_sd_card=True, features=frozenset({"ptz"}),
    )
    device = MagicMock()
    device.summary = AsyncMock(return_value=summary)
    session.device.return_value = device

    cameras = {c.device_id: c for c in await TapoCloudAccount(session).async_discover()}

    # Only the parent is probed: passthrough to a lens id is refused by the cloud.
    probed = [call.args[0] for call in session.device.call_args_list]
    assert probed == [PARENT]

    parent, fixed, pt = cameras[PARENT], cameras[PARENT + "00"], cameras[PARENT + "01"]
    assert (parent.stream_device_id, parent.channel, parent.is_lens) == (PARENT, 0, False)
    assert parent.has_ptz is True
    assert (fixed.stream_device_id, fixed.channel, fixed.is_lens) == (PARENT, 0, True)
    assert (pt.stream_device_id, pt.channel, pt.is_lens) == (PARENT, 1, True)
    assert fixed.name == "Fixed Lens" and pt.firmware == "1.0.0"
    # Motor and SD card belong to the parent device.
    assert pt.has_ptz is False and pt.has_sd_card is False


def _lens_info(channel):
    return CameraInfo(
        device_id=f"{PARENT}0{channel}", name=f"lens {channel}", model="DUALLENS",
        app_server="aps1", relay_device_id=PARENT, channel=channel,
    )


def _parent_info():
    return CameraInfo(device_id=PARENT, name="Driveway", model="DUALLENS", app_server="aps1")


def test_a_lens_handle_never_targets_its_own_thing_id():
    session = MagicMock()
    TapoCloudAccount(session).camera(_lens_info(1))
    assert session.device.call_args.args[0] == PARENT


async def test_a_lens_reads_its_state_from_the_parents_poll():
    session = MagicMock()
    device = MagicMock()
    device.device_info = AsyncMock(return_value={"device_model": "DUALLENS"})
    device.get_privacy_mode = AsyncMock(return_value=True)
    device.get_motion_detection = AsyncMock(return_value={"enabled": "on"})
    session.device.return_value = device
    account = TapoCloudAccount(session)
    account._discovered = {PARENT: _parent_info()}
    parent = account.camera(_parent_info())
    lens0, lens1 = account.camera(_lens_info(0)), account.camera(_lens_info(1))

    parent_state = await parent.async_poll()
    lens_states = [await lens0.async_poll(), await lens1.async_poll()]

    assert all(state == parent_state for state in lens_states)
    assert parent_state["available"] is True and parent_state["privacy_mode"] is True
    # The two lenses reused the parent's fresh poll rather than querying again.
    assert device.device_info.await_count == 1
    # And every passthrough handle that was created points at the parent.
    assert {call.args[0] for call in session.device.call_args_list} == {PARENT}


async def test_a_stale_parent_poll_is_refreshed_for_a_lens():
    session = MagicMock()
    device = MagicMock()
    device.device_info = AsyncMock(return_value={})
    device.get_privacy_mode = AsyncMock(return_value=False)
    device.get_motion_detection = AsyncMock(return_value={})
    session.device.return_value = device
    account = TapoCloudAccount(session)
    now = [1000.0]
    account._clock = lambda: now[0]
    parent, lens = account.camera(_parent_info()), account.camera(_lens_info(1))

    await parent.async_poll()
    now[0] += LENS_POLL_SHARE_SECONDS + 1
    await lens.async_poll()
    assert device.device_info.await_count == 2


async def test_a_lens_failure_is_not_masked_as_available_any_more():
    """The 0.1.1 'doesn't belong' workaround is gone; real failures show as such."""
    device = MagicMock()
    device.device_info = AsyncMock(side_effect=CloudError("Thing doesn't belong to user's account"))
    session = MagicMock()
    session.device.return_value = device
    state = await TapoCloudAccount(session).camera(_parent_info()).async_poll()
    assert state["available"] is False


# -- snapshots over the camera's own channel ----------------------------------

def _stream_of(*chunks, error=None):
    """A fake iter_live_ts that records its arguments."""
    calls = []

    def fake(session, device_id, *, channel=0, resolution="HD", idle_timeout=None):
        calls.append({"device_id": device_id, "channel": channel, "resolution": resolution})

        async def gen():
            if error is not None:
                raise error
            for chunk in chunks:
                yield chunk

        return gen()

    return fake, calls


async def test_a_lens_snapshot_streams_the_parent_on_the_lens_channel():
    fake, calls = _stream_of(b"\x47ts")
    camera = TapoCloudAccount(MagicMock()).camera(_lens_info(1))
    with (
        patch(f"{API}.iter_live_ts", fake),
        patch(f"{API}.decode_first_frame", AsyncMock(return_value=b"JPEG")) as decode,
    ):
        image = await camera.async_snapshot(ffmpeg="/opt/ffmpeg", seconds=0, resolution="VGA")

    assert image == b"JPEG"
    assert calls == [{"device_id": PARENT, "channel": 1, "resolution": "VGA"}]
    # Lens 1 is the second video stream in the relay's MPEG-TS.
    decode.assert_awaited_once_with(b"\x47ts", ffmpeg="/opt/ffmpeg", video_stream=1)


async def test_a_plain_camera_snapshot_decodes_the_first_stream():
    fake, _ = _stream_of(b"\x47ts")
    camera = TapoCloudAccount(MagicMock()).camera(_parent_info())
    with (
        patch(f"{API}.iter_live_ts", fake),
        patch(f"{API}.decode_first_frame", AsyncMock(return_value=b"J")) as decode,
    ):
        await camera.async_snapshot(seconds=0)
    assert decode.await_args.kwargs["video_stream"] == 0


async def test_snapshot_collects_seconds_of_video_before_decoding():
    fake, _ = _stream_of(b"a", b"b", b"c")
    camera = TapoCloudAccount(MagicMock()).camera(_parent_info())
    with (
        patch(f"{API}.iter_live_ts", fake),
        patch(f"{API}.decode_first_frame", AsyncMock(return_value=b"J")) as decode,
    ):
        await camera.async_snapshot(seconds=60)
    # The stream ended before 60 s, so everything received is decoded.
    assert decode.await_args.args[0] == b"abc"


async def test_snapshot_with_no_video_is_a_transport_error():
    fake, _ = _stream_of()
    camera = TapoCloudAccount(MagicMock()).camera(_parent_info())
    with patch(f"{API}.iter_live_ts", fake), pytest.raises(CloudTransportError):
        await camera.async_snapshot(seconds=0)


# -- token refresh throttling ------------------------------------------------

def _throttled_account():
    session = MagicMock()
    session.refresh = AsyncMock()
    account = TapoCloudAccount(session)
    now = [10_000.0]
    account._clock = lambda: now[0]
    return account, session, now


async def test_snapshot_renews_the_token_once_after_a_relay_401():
    account, session, _ = _throttled_account()
    camera = account.camera(_parent_info())
    attempts = iter([Unauthorized("relay request unauthorized"), None])

    def fake(session_, device_id, **kwargs):
        error = next(attempts)

        async def gen():
            if error:
                raise error
            yield b"ts"

        return gen()

    with (
        patch(f"{API}.iter_live_ts", fake),
        patch(f"{API}.decode_first_frame", AsyncMock(return_value=b"J")),
    ):
        assert await camera.async_snapshot(seconds=0) == b"J"
    session.refresh.assert_awaited_once()


async def test_a_permanent_401_renews_at_most_once_per_five_minutes():
    """The 0.1.1 behaviour renewed on every snapshot of every card, every 30 s."""
    account, session, now = _throttled_account()
    camera = account.camera(_parent_info())
    fake, _ = _stream_of(error=Unauthorized("relay request unauthorized"))

    with patch(f"{API}.iter_live_ts", fake):
        for _ in range(10):  # ten snapshots, 30 s apart: 4.5 minutes
            with pytest.raises(CloudAuthError):
                await camera.async_snapshot(seconds=0)
            now[0] += 30
        assert session.refresh.await_count == 1

        now[0] += TOKEN_REFRESH_MIN_INTERVAL  # well past the window
        with pytest.raises(CloudAuthError):
            await camera.async_snapshot(seconds=0)
        assert session.refresh.await_count == 2


async def test_a_coordinator_renewal_counts_towards_the_throttle():
    account, session, _ = _throttled_account()
    await account.async_refresh_token()  # e.g. the polling coordinator
    assert await account.async_refresh_token_throttled() is False
    session.refresh.assert_awaited_once()


async def test_snapshot_does_not_renew_for_errors_other_than_auth():
    account, session, _ = _throttled_account()
    camera = account.camera(_parent_info())
    fake, _ = _stream_of(error=RelayError("relay down"))
    with patch(f"{API}.iter_live_ts", fake), pytest.raises(CloudTransportError):
        await camera.async_snapshot(seconds=0)
    session.refresh.assert_not_awaited()


async def test_the_live_stream_renews_once_then_streams():
    account, session, _ = _throttled_account()
    camera = account.camera(_lens_info(1))
    attempts = iter([Unauthorized("relay request unauthorized"), None])

    def fake(session_, device_id, **kwargs):
        error = next(attempts)

        async def gen():
            if error:
                raise error
            yield b"live"

        return gen()

    with patch(f"{API}.iter_live_ts", fake):
        chunks = [chunk async for chunk in camera.iter_live_ts()]
    assert chunks == [b"live"]
    session.refresh.assert_awaited_once()


async def test_the_live_stream_respects_the_throttle():
    account, session, _ = _throttled_account()
    await account.async_refresh_token()  # renewed moments ago
    camera = account.camera(_parent_info())
    fake, _ = _stream_of(error=Unauthorized("relay request unauthorized"))
    with patch(f"{API}.iter_live_ts", fake), pytest.raises(Unauthorized):
        _ = [chunk async for chunk in camera.iter_live_ts()]
    session.refresh.assert_awaited_once()  # only the earlier one


async def test_decode_first_frame_with_real_ffmpeg():
    """End to end through a real ffmpeg, when one is installed."""
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        "-t", "1", "-c:v", "libx264", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot encode a test stream")

    image = await decode_first_frame(ts, ffmpeg=ffmpeg)
    assert image[:2] == b"\xff\xd8"  # a JPEG


async def test_decode_first_frame_reports_a_missing_ffmpeg():
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        await decode_first_frame(b"x", ffmpeg="/nonexistent/ffmpeg")


async def test_decode_picks_the_requested_video_stream_with_real_ffmpeg():
    """A multi-lens stream carries each lens as its own video stream."""
    import io
    import shutil

    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    make = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        "-f", "lavfi", "-i", "color=c=red:size=160x90:rate=10",
        "-t", "1", "-map", "0:v", "-map", "1:v", "-c:v", "libx264", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ts, _ = await make.communicate()
    if make.returncode != 0 or not ts:
        pytest.skip("this ffmpeg cannot build a two-stream test file")

    first = await decode_first_frame(ts, ffmpeg=ffmpeg)
    second = await decode_first_frame(ts, ffmpeg=ffmpeg, video_stream=1)
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (320, 240)
    with Image.open(io.BytesIO(second)) as image:
        assert image.size == (160, 90)


# -- media proof shared by a camera's lenses (0.1.8) ---------------------------

def test_video_from_any_lens_counts_for_its_physical_camera():
    account = TapoCloudAccount(MagicMock())
    now = [5000.0]
    account._clock = lambda: now[0]
    assert account.media_ok_within(PARENT, 300) is False

    account.mark_media_ok(PARENT)  # e.g. the PT lens streams its parent
    assert account.media_ok_within(PARENT, 300) is True
    now[0] += 301
    assert account.media_ok_within(PARENT, 300) is False


async def test_a_lens_snapshot_records_media_for_the_parent():
    account = TapoCloudAccount(MagicMock())
    fake, _ = _stream_of(b"\x47ts")
    with (
        patch(f"{API}.iter_live_ts", fake),
        patch(f"{API}.decode_first_frame", AsyncMock(return_value=b"J")),
    ):
        await account.camera(_lens_info(1)).async_snapshot(seconds=0)
    assert account.media_ok_within(PARENT, 60) is True


async def test_live_video_records_media():
    account = TapoCloudAccount(MagicMock())
    fake, _ = _stream_of(b"\x47live")
    with patch(f"{API}.iter_live_ts", fake):
        _ = [c async for c in account.camera(_lens_info(1)).iter_live_ts()]
    assert account.media_ok_within(PARENT, 60) is True


# -- motor commands and HTTP 408 (0.1.8) ---------------------------------------

def _motor_camera(pan_effects):
    session = MagicMock()
    device = MagicMock()
    device.pan = AsyncMock(side_effect=pan_effects)
    session.device.return_value = device
    return device, TapoCloudAccount(session).camera(_parent_info())


async def test_a_cloud_timeout_on_pan_is_retried(caplog):
    device, camera = _motor_camera([CloudError(408, "gateway timeout"), {}])
    with patch(f"{API}.MOTOR_RETRY_DELAYS", (0, 0)):
        await camera.async_pan(30)
    assert device.pan.await_count == 2
    assert "HTTP 408" in caplog.text and "gateway timeout" in caplog.text


async def test_repeated_timeouts_give_up_after_three_attempts(caplog):
    device, camera = _motor_camera([CloudError(408)] * 3)
    with patch(f"{API}.MOTOR_RETRY_DELAYS", (0, 0)), pytest.raises(CloudTransportError):
        await camera.async_pan(30)
    assert device.pan.await_count == 3
    assert "(empty response body)" in caplog.text


async def test_other_cloud_errors_are_not_retried():
    device, camera = _motor_camera([CloudError(400, "MODEL_SCHEMA_CHECK_FAILED")])
    with patch(f"{API}.MOTOR_RETRY_DELAYS", (0, 0)), pytest.raises(CloudSetterRejected):
        await camera.async_pan(30)
    assert device.pan.await_count == 1


async def test_an_unsupported_motor_is_raised_unchanged():
    device, camera = _motor_camera([UnsupportedMethod("motorMove")])
    with pytest.raises(UnsupportedMethod) as caught:
        await camera.async_pan(30)
    assert caught.value.__cause__ is not caught.value  # no self-referential cause


def test_cloud_errors_are_classified_by_status_code_not_message_text():
    """A 408 whose message happens to contain '400' is a timeout, not a refusal."""
    assert isinstance(_map_error(CloudError(408, "waited 4000 ms")), CloudTransportError)
    assert isinstance(_map_error(CloudError(400)), CloudSetterRejected)
