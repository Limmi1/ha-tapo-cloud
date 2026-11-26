"""Pan/tilt/preset and privacy services, addressed by camera entity."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .api import CloudSetterRejected, CloudTransportError, OnTapoError
from .const import (
    ATTR_ENABLED,
    ATTR_PRESET,
    ATTR_STEPS,
    DOMAIN,
    SERVICE_MOVE_TO_PRESET,
    SERVICE_PAN,
    SERVICE_SET_PRIVACY_MODE,
    SERVICE_TILT,
)
from .coordinator import TapoCloudCoordinator

SERVICE_PROBE_LENSES = "probe_lenses"

_LOGGER = logging.getLogger(__name__)

_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required(ATTR_STEPS): vol.All(vol.Coerce(int), vol.Range(min=-360, max=360)),
    }
)

_PRESET_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required(ATTR_PRESET): cv.string,
    }
)

_PROBE_SCHEMA = vol.Schema({vol.Optional("entity_id"): cv.entity_ids})

_PRIVACY_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required(ATTR_ENABLED): cv.boolean,
    }
)


def _coordinators_for(hass: HomeAssistant, call: ServiceCall) -> list[TapoCloudCoordinator]:
    """Resolve the target entity_ids to their coordinators."""
    registry = er.async_get(hass)
    found: list[TapoCloudCoordinator] = []
    for entity_id in call.data["entity_id"]:
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN:
            raise ServiceValidationError(f"{entity_id} is not a Tapo Cloud entity")
        for data in hass.data.get(DOMAIN, {}).values():
            for coordinator in data.coordinators.values():
                if entry.unique_id and entry.unique_id.startswith(
                    coordinator.info.device_id
                ):
                    found.append(coordinator)
                    break
    if not found:
        raise ServiceValidationError("No Tapo Cloud cameras matched that target")
    return found


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services once, on first entry setup."""
    if hass.services.has_service(DOMAIN, SERVICE_PAN):
        return

    async def _run(coordinator: TapoCloudCoordinator, action: str, *args: object) -> None:
        try:
            await getattr(coordinator.camera, action)(*args)
        except CloudSetterRejected as err:
            raise HomeAssistantError(
                "The Tapo cloud rejected this setting change before the camera "
                f"received it: {err}"
            ) from err
        except (CloudTransportError, OnTapoError) as err:
            raise HomeAssistantError(f"Tapo Cloud command failed: {err}") from err

    async def handle_pan(call: ServiceCall) -> None:
        for coordinator in _coordinators_for(hass, call):
            if not coordinator.info.has_ptz:
                raise ServiceValidationError(
                    f"{coordinator.info.name} has no pan/tilt motor"
                )
            await _run(coordinator, "async_pan", call.data[ATTR_STEPS])

    async def handle_tilt(call: ServiceCall) -> None:
        for coordinator in _coordinators_for(hass, call):
            if not coordinator.info.has_ptz:
                raise ServiceValidationError(
                    f"{coordinator.info.name} has no pan/tilt motor"
                )
            await _run(coordinator, "async_tilt", call.data[ATTR_STEPS])

    async def handle_preset(call: ServiceCall) -> None:
        for coordinator in _coordinators_for(hass, call):
            await _run(coordinator, "async_move_to_preset", call.data[ATTR_PRESET])

    async def handle_privacy(call: ServiceCall) -> None:
        for coordinator in _coordinators_for(hass, call):
            await _run(coordinator, "async_set_privacy_mode", call.data[ATTR_ENABLED])
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_PAN, handle_pan, schema=_MOVE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_TILT, handle_tilt, schema=_MOVE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_TO_PRESET, handle_preset, schema=_PRESET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PRIVACY_MODE, handle_privacy, schema=_PRIVACY_SCHEMA
    )

    async def handle_probe(call: ServiceCall) -> ServiceResponse:
        """Diagnostic: find which relay request delivers each extra lens."""
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        from .probe import probe_channel

        lenses = [
            coordinator
            for data in hass.data.get(DOMAIN, {}).values()
            for coordinator in data.coordinators.values()
            if coordinator.info.is_lens and coordinator.info.channel > 0
        ]
        if "entity_id" in call.data:
            wanted = {c.info.device_id for c in _coordinators_for(hass, call)}
            lenses = [c for c in lenses if c.info.device_id in wanted]
        seen: set[tuple[str, int]] = set()
        targets = []
        for coordinator in lenses:
            key = (coordinator.info.stream_device_id, coordinator.info.channel)
            if key not in seen:
                seen.add(key)
                targets.append(coordinator)
        if not targets:
            raise ServiceValidationError(
                "No multi-lens camera with a second lens was found to probe"
            )

        ffmpeg = get_ffmpeg_manager(hass).binary
        save_dir = Path(hass.config.path("www", "tapo_probe"))
        results = []
        for coordinator in targets:
            info = coordinator.info
            for result in await probe_channel(
                hass,
                coordinator.account.session,
                info.stream_device_id,
                info.channel,
                ffmpeg=ffmpeg,
                save_dir=save_dir,
            ):
                result["camera"] = info.name
                _LOGGER.warning(
                    "Tapo lens probe | %s | %s | %s%s",
                    info.name,
                    result["variant"],
                    result["outcome"],
                    f" (difference {result['difference']}, {result['size']})"
                    if "difference" in result
                    else "",
                )
                results.append(result)
        return {"results": results}

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROBE_LENSES,
        handle_probe,
        schema=_PROBE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the services when the last entry goes away."""
    for service in (
        SERVICE_PAN,
        SERVICE_TILT,
        SERVICE_MOVE_TO_PRESET,
        SERVICE_SET_PRIVACY_MODE,
        SERVICE_PROBE_LENSES,
    ):
        hass.services.async_remove(DOMAIN, service)
