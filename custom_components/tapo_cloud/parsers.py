"""Pure functions that turn raw device replies into entity state.

Kept free of Home Assistant imports so they can be unit-tested on their own,
and so the cloud connection mode stays portable to another integration.

Tapo models disagree about where they nest a value: one camera answers
``{"motion_detection": {"motion_det": {"enabled": "on"}}}`` and another omits a
level. Rather than hardcode one shape per model, these helpers search for the
meaningful leaf and return ``None`` when the camera did not report it at all —
which the entity layer renders as unavailable rather than as a false "off".
"""

from __future__ import annotations

from typing import Any

_TRUTHY = frozenset({"on", "true", "1", "enable", "enabled", "yes"})
_FALSEY = frozenset({"off", "false", "0", "disable", "disabled", "no"})


def find_key(obj: Any, key: str) -> Any:
    """Depth-first search for ``key`` in nested dicts and lists. ``None`` if absent."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def as_bool(value: Any) -> bool | None:
    """Coerce a device flag to a bool. ``None`` when it is not a flag at all."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSEY:
            return False
    return None


def privacy_enabled(data: dict[str, Any]) -> bool | None:
    """Whether the lens mask is on."""
    value = data.get("privacy_mode")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return as_bool(find_key(value, "enabled"))
    return as_bool(value)


def motion_detection_enabled(data: dict[str, Any]) -> bool | None:
    """Whether the camera's own motion detection is switched on."""
    raw = data.get("motion_detection")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return as_bool(find_key(raw, "enabled"))


def sd_card_problem(data: dict[str, Any]) -> bool | None:
    """``True`` when a camera that should have a usable SD card does not."""
    card = data.get("sd_card")
    if card is None:
        return None
    healthy = getattr(card, "healthy", None)
    if healthy is None:
        return None
    return not bool(healthy)
