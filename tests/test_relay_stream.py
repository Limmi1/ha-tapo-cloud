"""The continuous live-stream generator, driven against a fake relay.

These tests also pin the coupling to OnTapo's internals: if a future OnTapo
release renames one of the helpers this module reuses,
test_compatibility_guard_lists_real_symbols fails loudly here rather than in a
user's living room.
"""

from __future__ import annotations

import asyncio

import pytest
from ontapo import RelayError
from ontapo.relay import RelayParams

from custom_components.tapo_cloud import relay_stream
from custom_components.tapo_cloud.relay_stream import (
    IncompatibleOnTapo,
    assert_ontapo_compatible,
    iter_live_ts,
)

RELAY = RelayParams(
    relay_ip="203.0.113.10",
    relay_token="token=abc;tokenType=cipcAuthToken",
    relay_url="https://relay.example.com/relayservice?route=xyz",
)


class FakeSession:
    token = "t"
    terminal_uuid = "UUID"


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.fixture
def fake_relay(monkeypatch):
    """Patch OnTapo's relay negotiation and framing with scripted answers."""
    from ontapo import stream as ontapo_stream

    state: dict[str, object] = {"parts": [], "status": 200, "writer": FakeWriter()}

    async def fake_request_relay(session, device_id, stream_type, track_id):
        state["requested"] = (device_id, int(stream_type), track_id)
        return RELAY

    async def fake_open_relay(relay, terminal_uuid, track_id):
        return object(), state["writer"], "relay.example.com", "/relayservice?route=xyz"

    async def fake_read_status(reader):
        return state["status"]

    async def fake_read_headers(reader):
        return {"x-request-type": "preview"}

    async def fake_read_part(reader):
        parts: list = state["parts"]  # type: ignore[assignment]
        if not parts:
            raise asyncio.IncompleteReadError(b"", None)
        item = parts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(relay_stream, "request_relay", fake_request_relay)
    monkeypatch.setattr(ontapo_stream, "_open_relay", fake_open_relay)
    monkeypatch.setattr(ontapo_stream, "_read_status", fake_read_status)
    monkeypatch.setattr(ontapo_stream, "_read_headers", fake_read_headers)
    monkeypatch.setattr(ontapo_stream, "_read_part", fake_read_part)
    return state


async def _collect(session=None, **kwargs) -> list[bytes]:
    return [
        chunk async for chunk in iter_live_ts(session or FakeSession(), "DEV1", **kwargs)
    ]


async def test_yields_video_parts_and_stops_at_end_of_stream(fake_relay):
    fake_relay["parts"] = [
        ("video/mp2t", b"\x47chunk1"),
        ("video/mp2t", b"\x47chunk2"),
    ]
    assert await _collect() == [b"\x47chunk1", b"\x47chunk2"]
    assert fake_relay["writer"].closed is True


async def test_requests_the_preview_stream_type(fake_relay):
    fake_relay["parts"] = [("video/mp2t", b"\x47x")]
    await _collect()
    device_id, stream_type, track_id = fake_relay["requested"]
    assert device_id == "DEV1"
    assert stream_type == 0  # StreamType.PREVIEW — live, not playback
    assert track_id == "ha-preview-DEV1"


async def test_control_frame_asks_for_the_requested_resolution(fake_relay):
    fake_relay["parts"] = [("video/mp2t", b"\x47x")]
    await _collect(resolution="VGA")
    assert b'"resolutions":["VGA"]' in fake_relay["writer"].written


async def test_json_control_parts_are_not_yielded_as_video(fake_relay):
    fake_relay["parts"] = [
        ("application/json", b'{"type":"notification","seq":1}'),
        ("video/mp2t", b"\x47only-video"),
    ]
    assert await _collect() == [b"\x47only-video"]


async def test_relay_refusal_raises(fake_relay):
    fake_relay["parts"] = [
        ("application/json", b'{"error_code":-52405}'),
    ]
    with pytest.raises(RelayError):
        await _collect()


async def test_non_200_relay_response_raises(fake_relay):
    fake_relay["status"] = 403
    with pytest.raises(RelayError, match="403"):
        await _collect()


async def test_no_data_at_all_raises_rather_than_ending_quietly(fake_relay):
    fake_relay["parts"] = []
    with pytest.raises(RelayError, match="before any data"):
        await _collect()


async def test_idle_timeout_after_data_ends_cleanly(fake_relay):
    """A stalled relay after real video is an end-of-stream, not an error."""
    fake_relay["parts"] = [("video/mp2t", b"\x47x"), TimeoutError()]
    assert await _collect() == [b"\x47x"]


async def test_writer_is_closed_even_when_the_consumer_stops_early(fake_relay):
    fake_relay["parts"] = [("video/mp2t", b"\x47a"), ("video/mp2t", b"\x47b")]
    agen = iter_live_ts(FakeSession(), "DEV1")
    assert await agen.__anext__() == b"\x47a"
    await agen.aclose()
    assert fake_relay["writer"].closed is True


def test_compatibility_guard_lists_real_symbols():
    """Every internal we reuse must actually exist in the installed OnTapo."""
    assert_ontapo_compatible()


def test_compatibility_guard_fails_on_a_missing_internal(monkeypatch):
    from ontapo import stream as ontapo_stream

    monkeypatch.delattr(ontapo_stream, "_read_part")
    with pytest.raises(IncompatibleOnTapo, match="_read_part"):
        assert_ontapo_compatible()
