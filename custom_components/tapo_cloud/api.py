"""Cloud connection mode: a narrow adapter over OnTapo.

Everything Home Assistant needs from the Tapo cloud goes through this module,
and nothing in here imports Home Assistant. That is deliberate: the integration
is meant to be contributable to HomeAssistant-Tapo-Control as an optional
*cloud* connection mode alongside its existing local (pytapo) mode, so the
transport has to be separable from the entity layer.

The contract an alternative transport would have to satisfy is
:class:`TapoCloudAccount` + :class:`TapoCloudCamera` below.

Security notes:
  * Credentials are used once, at login, and are not retained by this module.
  * The session token and refresh token are the only persisted secrets; they are
    never logged, and :func:`redact` is used for diagnostics.
  * All traffic is TLS to ``*.tplinkcloud.com``; OnTapo pins TP-Link's private
    CA in addition to the system trust store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ontapo import (
    AuthError,
    CloudError,
    DeviceError,
    MFARequired,
    OnTapo,
    OnTapoError,
    RelayError,
    Unauthorized,
    UnsupportedMethod,
)

from .relay_stream import assert_ontapo_compatible, iter_live_ts

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "TapoCloudAccount",
    "TapoCloudCamera",
    "CameraInfo",
    "SessionTokens",
    "CloudAuthError",
    "CloudMfaRequired",
    "CloudTransportError",
    "CloudSetterRejected",
    "redact",
    "async_send_mfa_code",
    "async_submit_mfa",
    "AuthError",
    "CloudError",
    "DeviceError",
    "OnTapoError",
    "RelayError",
    "Unauthorized",
    "UnsupportedMethod",
]

_SECRET_KEYS = frozenset(
    {"token", "refresh_token", "password", "relay_token", "relayToken"}
)


def redact(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data`` with secret values replaced, for logs and diagnostics."""
    return {
        key: ("**REDACTED**" if key in _SECRET_KEYS and value else value)
        for key, value in data.items()
    }


class CloudAuthError(Exception):
    """The account credentials or the stored token were rejected."""


class CloudMfaRequired(Exception):
    """The account needs a second factor to finish logging in."""


class CloudTransportError(Exception):
    """The cloud gateway or the media relay could not be reached."""


class CloudSetterRejected(Exception):
    """The cloud gateway refused a write before the camera saw it.

    TP-Link's gateway validates method names against a per-model schema and
    rejects every ``set*`` call with ``MODEL_SCHEMA_CHECK_FAILED``. Motor moves
    are whitelisted and unaffected. See docs/VERIFICATION.md.
    """


@dataclass(frozen=True, slots=True)
class SessionTokens:
    """The persistable part of a logged-in session. Treat as a credential."""

    token: str
    refresh_token: str
    terminal_uuid: str
    app_server: str
    account_base: str

    def as_dict(self) -> dict[str, str]:
        return {
            "token": self.token,
            "refresh_token": self.refresh_token,
            "terminal_uuid": self.terminal_uuid,
            "app_server": self.app_server,
            "account_base": self.account_base,
        }


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """A camera discovered on the account."""

    device_id: str
    name: str
    model: str
    app_server: str
    firmware: str = ""
    has_ptz: bool = False
    has_sd_card: bool = False
    features: frozenset[str] = field(default_factory=frozenset)


def _map_error(err: Exception) -> Exception:
    """Translate an OnTapo error into this module's vocabulary."""
    if isinstance(err, MFARequired):
        return CloudMfaRequired(str(err))
    if isinstance(err, (AuthError, Unauthorized)):
        return CloudAuthError(str(err))
    if isinstance(err, CloudError):
        text = str(err)
        if "MODEL_SCHEMA_CHECK_FAILED" in text or "400" in text:
            return CloudSetterRejected(text)
        return CloudTransportError(text)
    if isinstance(err, RelayError):
        return CloudTransportError(str(err))
    return err


async def async_send_mfa_code(
    session: OnTapo, password: str, mfa_type: int = 2
) -> None:
    """Ask the Tapo cloud to send a verification code, the way it now requires.

    Two deviations from ``OnTapo.send_mfa_code()`` (ontapo 0.1.0), both found by
    testing against the live server (see docs/VERIFICATION.md):

    * **The account password must be re-sent** as ``cloudPassword`` on this
      request. Without it the server rejects the call with ``-20615 Password
      format error`` and sends no code — so the caller waits for a code that was
      never dispatched.
    * **The failure is reported as HTTP 200** with an ``error_code`` in the
      body, so the status code alone cannot be trusted; the body is checked.

    Reuses OnTapo's own request signing. Raises :class:`CloudAuthError` with the
    server's own code on refusal, :class:`CloudTransportError` otherwise.
    """
    import json as _json

    path = "/api/v2/account/getPushVC4TerminalMFA"
    body = _json.dumps(
        {
            "appType": "TP-Link_Tapo_Android",
            "cloudUserName": session.email,
            "cloudPassword": password,
            "MFAProcessId": session._mfa_process_id,
            "MFAType": mfa_type,
            "terminalUUID": session.terminal_uuid,
        }
    ).encode()
    try:
        resp = await session._client.post(
            session.account_base + path,
            content=body,
            headers=session._signed_headers(path, body),
        )
    except Exception as err:  # noqa: BLE001
        raise CloudTransportError(f"could not request an MFA code: {err}") from err

    if resp.status_code != 200:
        raise CloudTransportError(f"MFA code request http {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as err:
        raise CloudTransportError("MFA code request returned no JSON") from err
    code = int(data.get("error_code", 0) or 0)
    if code:
        raise CloudAuthError(
            f"the server would not send a code (error_code {code}: "
            f"{data.get('msg') or 'no message'})"
        )


async def async_submit_mfa(session: OnTapo, code: str, mfa_type: int = 2) -> None:
    """Complete login with a verification code.

    The code is reduced to digits first: the Tapo server rejects a grouped code
    like ``150-791`` with ``-20106 (value out of range)``, so spaces, hyphens
    and other separators a user may copy in are stripped.
    """
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        raise CloudAuthError("the verification code contained no digits")
    try:
        await session.submit_mfa(digits, mfa_type)
    except Exception as err:  # noqa: BLE001
        raise _map_error(err) from err


class TapoCloudAccount:
    """A logged-in Tapo cloud account: discovery plus per-camera handles."""

    def __init__(self, session: OnTapo, *, relay_host: str | None = None) -> None:
        self._session = session
        self._lock = asyncio.Lock()
        if relay_host:
            self._apply_relay_host(relay_host)

    @staticmethod
    def _apply_relay_host(host: str) -> None:
        """Override OnTapo's hardcoded ap-southeast-1 relay host.

        OnTapo 0.1.0 pins ``CIPC_HOST`` at module level with no parameter, so a
        module attribute is the only seam. Harmless and reversible; isolated
        here so it is easy to drop once OnTapo takes a region argument.
        """
        from ontapo import relay as _relay

        if host and host != _relay.CIPC_HOST:
            _LOGGER.debug("overriding relay negotiation host with %s", host)
            _relay.CIPC_HOST = host

    @property
    def session(self) -> OnTapo:
        return self._session

    @property
    def tokens(self) -> SessionTokens:
        s = self._session
        return SessionTokens(
            token=s.token,
            refresh_token=getattr(s, "refresh_token", "") or "",
            terminal_uuid=s.terminal_uuid,
            app_server=getattr(s, "app_server", "") or "",
            account_base=getattr(s, "account_base", "") or "",
        )

    # -- construction ---------------------------------------------------

    @classmethod
    async def login(
        cls,
        email: str,
        password: str,
        *,
        mfa: Callable[[], str | Awaitable[str]] | None = None,
        relay_host: str | None = None,
    ) -> TapoCloudAccount:
        """Log in with account credentials.

        Raises:
            CloudMfaRequired: the account needs a second factor. When ``mfa`` is
                omitted, the partially-authenticated session is attached to the
                raised :class:`ontapo.MFARequired` so a config flow can drive
                the code exchange itself.
            CloudAuthError: the credentials were rejected.
        """
        assert_ontapo_compatible()
        try:
            session = await OnTapo.login(email, password, mfa=mfa)
        except MFARequired:
            raise
        except Exception as err:  # noqa: BLE001 - translated below
            raise _map_error(err) from err
        return cls(session, relay_host=relay_host)

    @classmethod
    def from_tokens(
        cls, tokens: SessionTokens, *, relay_host: str | None = None
    ) -> TapoCloudAccount:
        """Rebuild a session from persisted tokens, with no login or MFA."""
        assert_ontapo_compatible()
        session = OnTapo.from_dict(tokens.as_dict())
        return cls(session, relay_host=relay_host)

    async def async_close(self) -> None:
        await self._session.aclose()

    async def async_refresh_token(self) -> SessionTokens:
        """Renew the session token. Raises :class:`CloudAuthError` if refused."""
        try:
            await self._session.refresh()
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err
        return self.tokens

    # -- discovery ------------------------------------------------------

    async def async_discover(self) -> list[CameraInfo]:
        """List every camera on the account, with its capabilities.

        Capability probing is best-effort per camera: a camera that fails to
        answer still appears, so it can be shown as unavailable rather than
        vanishing from the dashboard.
        """
        try:
            cameras = await self._session.cameras()
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err

        async def describe(cam: Any) -> CameraInfo:
            base = CameraInfo(
                device_id=cam.device_id,
                name=cam.name or cam.model or cam.device_id,
                model=cam.model,
                app_server=cam.app_server,
            )
            try:
                device = self._session.device(cam.device_id, name=cam.name)
                summary = await device.summary()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("capability probe failed for %s: %s", cam.device_id, err)
                return base
            return CameraInfo(
                device_id=base.device_id,
                name=summary.name or base.name,
                model=summary.model or base.model,
                app_server=base.app_server,
                firmware=summary.firmware,
                has_ptz=summary.has_ptz,
                has_sd_card=summary.has_sd_card,
                features=frozenset(summary.features),
            )

        return list(await asyncio.gather(*(describe(cam) for cam in cameras)))

    def camera(self, info: CameraInfo) -> TapoCloudCamera:
        """A control handle for one discovered camera."""
        return TapoCloudCamera(self, info)


class TapoCloudCamera:
    """One camera, reached over the cloud relay."""

    def __init__(self, account: TapoCloudAccount, info: CameraInfo) -> None:
        self._account = account
        self._info = info
        self._device = account.session.device(info.device_id, name=info.name)
        # The relay fans one live stream out to many viewers, but our own
        # negotiation is serialised so a dashboard opening two cards at once
        # cannot race two relay requests for the same track id.
        self._stream_lock = asyncio.Lock()

    @property
    def info(self) -> CameraInfo:
        return self._info

    @property
    def device_id(self) -> str:
        return self._info.device_id

    # -- media ----------------------------------------------------------

    def iter_live_ts(self, *, resolution: str = "HD") -> AsyncIterator[bytes]:
        """An async iterator of MPEG-TS chunks of the live view.

        Remote by construction: relay-negotiated, no LAN path to the camera.
        """
        return iter_live_ts(
            self._account.session, self.device_id, resolution=resolution
        )

    async def async_snapshot(self, out_path: str, *, seconds: float = 4.0) -> str:
        """Decode one still frame of the live view to ``out_path``.

        The camera exposes no still-image endpoint, so OnTapo pulls a few
        seconds of live video and decodes a frame with ffmpeg.
        """
        try:
            return await self._device.snapshot(out_path, seconds=seconds)
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err

    # -- state ----------------------------------------------------------

    async def async_poll(self) -> dict[str, Any]:
        """Read the state this integration surfaces as entities.

        Each read is independent and best-effort: a camera model that lacks a
        feature answers ``-40101`` (``UnsupportedMethod``), which is recorded as
        ``None`` rather than failing the whole poll.
        """
        state: dict[str, Any] = {"available": False}

        async def attempt(key: str, coro: Awaitable[Any]) -> None:
            try:
                state[key] = await coro
            except UnsupportedMethod:
                state[key] = None
            except (DeviceError, CloudError, OnTapoError) as err:
                _LOGGER.debug("%s read failed for %s: %s", key, self.device_id, err)
                state[key] = None

        await attempt("device_info", self._device.device_info())
        state["available"] = state.get("device_info") is not None
        if not state["available"]:
            return state

        await attempt("privacy_mode", self._device.get_privacy_mode())
        await attempt("motion_detection", self._device.get_motion_detection())
        if self._info.has_sd_card:
            await attempt("sd_card", self._device.sd_card_status())
        return state

    # -- controls -------------------------------------------------------

    async def async_pan(self, steps: int) -> None:
        """Pan by relative ``steps``. ``motorMove`` is gateway-whitelisted."""
        await self._call(self._device.pan(steps))

    async def async_tilt(self, steps: int) -> None:
        """Tilt by relative ``steps``; positive tilts the view up."""
        await self._call(self._device.tilt(steps))

    async def async_move_to_preset(self, preset: str) -> None:
        await self._call(self._device.move_to_preset(preset))

    async def async_presets(self) -> list[tuple[str, str]]:
        try:
            return [(p.id, p.name) for p in await self._device.presets()]
        except UnsupportedMethod:
            return []
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err

    async def async_set_privacy_mode(self, enabled: bool) -> None:
        """Attempt to set privacy mode.

        Raises :class:`CloudSetterRejected` on the current gateway, which
        refuses every ``set*`` method before the camera sees it. Implemented and
        kept because the rejection is region- and model-dependent and may lift.
        """
        await self._call(self._device.set_privacy_mode(enabled))

    async def async_set_motion_detection(self, enabled: bool) -> None:
        """Attempt to set motion detection. See :meth:`async_set_privacy_mode`."""
        await self._call(self._device.set_motion_detection(enabled))

    async def _call(self, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err
