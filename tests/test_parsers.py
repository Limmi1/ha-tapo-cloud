"""The device-reply parsers: nesting varies by model, absence is not 'off'."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.tapo_cloud.parsers import (
    as_bool,
    find_key,
    motion_detection_enabled,
    privacy_enabled,
    sd_card_problem,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("on", True),
        ("ON", True),
        (" off ", False),
        ("enabled", True),
        ("disabled", False),
        (True, True),
        (0, False),
        (1, True),
        ("maybe", None),
        (None, None),
        ([], None),
    ],
)
def test_as_bool(value, expected):
    assert as_bool(value) is expected


def test_find_key_searches_nested_dicts_and_lists():
    payload = {"a": {"b": [{"c": {"enabled": "on"}}]}}
    assert find_key(payload, "enabled") == "on"
    assert find_key(payload, "absent") is None


def test_privacy_accepts_plain_bool_and_nested_shapes():
    assert privacy_enabled({"privacy_mode": True}) is True
    assert privacy_enabled({"privacy_mode": "off"}) is False
    assert privacy_enabled({"privacy_mode": {"lens_mask": {"enabled": "on"}}}) is True


def test_privacy_missing_is_unknown_not_off():
    """A camera that did not answer must read as unavailable, never as 'off'."""
    assert privacy_enabled({}) is None
    assert privacy_enabled({"privacy_mode": None}) is None


def test_motion_detection_shapes():
    assert motion_detection_enabled({"motion_detection": {"motion_det": {"enabled": "on"}}})
    assert (
        motion_detection_enabled({"motion_detection": {"enabled": "off"}}) is False
    )
    assert motion_detection_enabled({"motion_detection": None}) is None
    assert motion_detection_enabled({}) is None


def test_sd_card_problem_inverts_health():
    assert sd_card_problem({"sd_card": SimpleNamespace(healthy=True)}) is False
    assert sd_card_problem({"sd_card": SimpleNamespace(healthy=False)}) is True
    assert sd_card_problem({"sd_card": None}) is None
    assert sd_card_problem({}) is None
