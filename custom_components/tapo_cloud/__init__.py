"""The Tapo Cloud integration.

Connects TP-Link Tapo cameras to Home Assistant through the owner's Tapo cloud
account, over TP-Link's own media relay. Cameras may sit behind any number of
separate routers and internet connections; no VPN, port forwarding or gateway
hardware is involved, and no camera-local password is needed.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import (
    CloudAuthError,
    CloudTransportError,
    SessionTokens,
    TapoCloudAccount,
)
from .const import (
    CONF_ACCOUNT_BASE,
    CONF_APP_SERVER,
    CONF_REFRESH_TOKEN,
    CONF_RELAY_HOST,
    CONF_TERMINAL_UUID,
    CONF_TOKEN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import TapoCloudCoordinator
from .relay_stream import IncompatibleOnTapo, assert_ontapo_compatible
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

class TapoCloudData:
    """Runtime data for one config entry: the account and a coordinator each."""

    def __init__(
        self,
        account: TapoCloudAccount,
        coordinators: dict[str, TapoCloudCoordinator],
    ) -> None:
        self.account = account
        self.coordinators = coordinators


def _tokens_from_entry(entry: ConfigEntry) -> SessionTokens:
    return SessionTokens(
        token=entry.data[CONF_TOKEN],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN, ""),
        terminal_uuid=entry.data[CONF_TERMINAL_UUID],
        app_server=entry.data.get(CONF_APP_SERVER, ""),
        account_base=entry.data.get(CONF_ACCOUNT_BASE, ""),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tapo Cloud from a config entry."""
    try:
        assert_ontapo_compatible()
    except IncompatibleOnTapo as err:
        raise ConfigEntryNotReady(str(err)) from err

    account = TapoCloudAccount.from_tokens(
        _tokens_from_entry(entry),
        relay_host=entry.options.get(CONF_RELAY_HOST)
        or entry.data.get(CONF_RELAY_HOST),
    )

    try:
        cameras = await account.async_discover()
    except CloudAuthError:
        # Try one refresh before asking the user to sign in again.
        try:
            tokens = await account.async_refresh_token()
        except CloudAuthError as refresh_err:
            await account.async_close()
            raise ConfigEntryAuthFailed(str(refresh_err)) from refresh_err
        _persist_tokens(hass, entry, tokens)
        try:
            cameras = await account.async_discover()
        except CloudAuthError as retry_err:
            await account.async_close()
            raise ConfigEntryAuthFailed(str(retry_err)) from retry_err
        except CloudTransportError as retry_err:
            await account.async_close()
            raise ConfigEntryNotReady(str(retry_err)) from retry_err
        _LOGGER.debug("recovered entry %s with a refreshed token", entry.title)
    except CloudTransportError as err:
        await account.async_close()
        raise ConfigEntryNotReady(str(err)) from err

    if not cameras:
        _LOGGER.warning("No Tapo cameras found on account %s", entry.title)

    coordinators: dict[str, TapoCloudCoordinator] = {}
    for info in cameras:
        coordinator = TapoCloudCoordinator(hass, account, info)
        coordinators[info.device_id] = coordinator

    await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in coordinators.values()),
        return_exceptions=True,
    )

    entry.runtime_data = TapoCloudData(account, coordinators)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry.runtime_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


def _persist_tokens(
    hass: HomeAssistant, entry: ConfigEntry, tokens: SessionTokens
) -> None:
    """Write a renewed token back to the entry. Never logged."""
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_TOKEN: tokens.token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_APP_SERVER: tokens.app_server,
            CONF_ACCOUNT_BASE: tokens.account_base,
        },
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data: TapoCloudData | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if data is not None:
            await data.account.async_close()
        if not hass.data.get(DOMAIN):
            async_unload_services(hass)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)
