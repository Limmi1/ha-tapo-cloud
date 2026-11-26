"""Graphical configuration flow: credentials, then MFA if the account asks."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from ontapo import MFARequired

from .api import (
    CloudAuthError,
    CloudTransportError,
    TapoCloudAccount,
    async_send_mfa_code,
    async_submit_mfa,
)
from .const import (
    CONF_ACCOUNT_BASE,
    CONF_APP_SERVER,
    CONF_EMAIL,
    CONF_LIVE_FPS,
    CONF_LIVE_WIDTH,
    CONF_REFRESH_TOKEN,
    CONF_RELAY_HOST,
    CONF_RESOLUTION,
    CONF_SNAPSHOT_INTERVAL,
    CONF_TERMINAL_UUID,
    CONF_TOKEN,
    DEFAULT_LIVE_FPS,
    DEFAULT_LIVE_WIDTH,
    DEFAULT_RELAY_HOST,
    DEFAULT_RESOLUTION,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    KNOWN_RELAY_HOSTS,
    RESOLUTIONS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)

STEP_MFA_SCHEMA = vol.Schema(
    {
        vol.Required("code"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        )
    }
)


class TapoCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Tapo Cloud config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        # Held only in memory for the duration of the flow: the MFA code-send
        # request has to re-present the password (the server rejects it
        # otherwise). It is never written to the config entry.
        self._password: str = ""
        self._account: TapoCloudAccount | None = None
        self._pending_session: Any = None
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the Tapo account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            try:
                self._account = await TapoCloudAccount.login(
                    self._email, self._password
                )
            except MFARequired as err:
                # The partially-authenticated session is attached to the error;
                # it is already region-adopted, so the code exchange continues
                # on it rather than logging in again.
                self._pending_session = getattr(err, "session", None)
                if self._pending_session is None:
                    errors["base"] = "mfa_unsupported"
                else:
                    return await self.async_step_mfa()
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudTransportError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during Tapo cloud login")
                errors["base"] = "unknown"
            else:
                return await self._async_finish()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the emailed verification code for a session token."""
        errors: dict[str, str] = {}
        session = self._pending_session
        assert session is not None

        if user_input is None:
            try:
                await async_send_mfa_code(session, self._password)
            except CloudAuthError:
                # The server refused to send a code — almost always a stale or
                # wrong password reaching this endpoint. Send the user back.
                _LOGGER.debug("MFA code send refused by the server")
                errors["base"] = "mfa_send_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("could not request an MFA code")
                errors["base"] = "mfa_send_failed"
        else:
            try:
                await async_submit_mfa(session, user_input["code"])
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("MFA code rejected: %s", err)
                errors["base"] = "invalid_code"
            else:
                self._account = TapoCloudAccount(session)
                return await self._async_finish()

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        """Verify the session works, then store only tokens — never the password."""
        assert self._account is not None
        try:
            cameras = await self._account.async_discover()
        except CloudAuthError:
            await self._account.async_close()
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_SCHEMA, errors={"base": "invalid_auth"}
            )
        except CloudTransportError:
            await self._account.async_close()
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "cannot_connect"},
            )

        tokens = self._account.tokens
        await self._account.async_close()

        data = {
            CONF_EMAIL: self._email,
            CONF_TOKEN: tokens.token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_TERMINAL_UUID: tokens.terminal_uuid,
            CONF_APP_SERVER: tokens.app_server,
            CONF_ACCOUNT_BASE: tokens.account_base,
        }

        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data={**self._reauth_entry.data, **data}
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(self._email.lower())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._email,
            data=data,
            description_placeholders={"count": str(len(cameras))},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """The stored token expired and could not be refreshed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=STEP_USER_SCHEMA,
                description_placeholders={"email": self._email},
            )
        return await self.async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TapoCloudOptionsFlow()


class TapoCloudOptionsFlow(OptionsFlow):
    """Stream quality, snapshot cadence and the relay region."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RESOLUTION,
                    default=options.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=RESOLUTIONS, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(
                    CONF_SNAPSHOT_INTERVAL,
                    default=options.get(
                        CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=10, max=600, step=5, unit_of_measurement="s")
                ),
                vol.Optional(
                    CONF_LIVE_WIDTH,
                    default=options.get(CONF_LIVE_WIDTH, DEFAULT_LIVE_WIDTH),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=3840, step=16, unit_of_measurement="px")
                ),
                vol.Optional(
                    CONF_LIVE_FPS,
                    default=options.get(CONF_LIVE_FPS, DEFAULT_LIVE_FPS),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=30, step=1, unit_of_measurement="fps")
                ),
                vol.Optional(
                    CONF_RELAY_HOST,
                    default=options.get(CONF_RELAY_HOST, DEFAULT_RELAY_HOST),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=KNOWN_RELAY_HOSTS,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
