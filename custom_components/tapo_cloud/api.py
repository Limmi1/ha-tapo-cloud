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
import time
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
    "map_lenses",
    "decode_first_frame",
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
    """A camera discovered on the account.

    A multi-lens camera (a dual-lens model, for example) is listed by the
    cloud as a parent thing plus one thing per lens, whose id is the parent's
    id with a two-digit lens number appended. The cloud refuses passthrough to
    a lens thing, and the relay refuses to stream it; the Tapo app instead
    streams the **parent** device on the lens's **channel**. So for a lens,
    ``relay_device_id`` is the parent's id and ``channel`` the lens number,
    and every cloud call for it goes to the parent.
    """

    device_id: str
    name: str
    model: str
    app_server: str
    firmware: str = ""
    has_ptz: bool = False
    has_sd_card: bool = False
    features: frozenset[str] = field(default_factory=frozenset)
    # Empty for an ordinary camera, meaning "use device_id".
    relay_device_id: str = ""
    channel: int = 0

    @property
    def stream_device_id(self) -> str:
        """The device id to send relay and passthrough calls to."""
        return self.relay_device_id or self.device_id

    @property
    def is_lens(self) -> bool:
        """Whether this is one lens of a multi-lens camera."""
        return bool(self.relay_device_id) and self.relay_device_id != self.device_id


def map_lenses(device_ids: list[str]) -> dict[str, tuple[str, int]]:
    """Find lens things: ``{lens_id: (parent_id, channel)}``.

    A thing is a lens when its id is another discovered thing's id plus exactly
    two digits; the digits are the channel (``"00"`` is 0, ``"01"`` is 1).
    Only the id with its last two characters removed can be the parent, so a
    match is never ambiguous.
    """
    ids = set(device_ids)
    lenses: dict[str, tuple[str, int]] = {}
    for thing in device_ids:
        if len(thing) < 3:
            continue
        parent, suffix = thing[:-2], thing[-2:]
        if suffix.isascii() and suffix.isdigit() and parent in ids:
            lenses[thing] = (parent, int(suffix))
    return lenses


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


# A media path that keeps getting refused renews the account token at most
# this often (seconds), account-wide.
TOKEN_REFRESH_MIN_INTERVAL = 300.0

# How old a parent camera's poll may be for a lens to reuse it (seconds). The
# coordinators all poll on the same interval, so a lens nearly always finds its
# parent's result from moments earlier.
LENS_POLL_SHARE_SECONDS = 60.0


async def decode_first_frame(
    video: bytes, *, ffmpeg: str = "ffmpeg", video_stream: int = 0
) -> bytes:
    """Decode the first video frame of an MPEG-TS buffer to JPEG bytes.

    Uses pipes for both input and output, so nothing touches the filesystem.
    ``video_stream`` picks which video stream to decode when the buffer holds
    more than one. Raises ``RuntimeError`` if ffmpeg is missing or produces no
    image.
    """
    select = ("-map", f"0:v:{int(video_stream)}") if video_stream else ()
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-f", "mpegts", "-i", "pipe:0",
            *select,
            "-frames:v", "1", "-q:v", "3",
            "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as err:
        raise RuntimeError(f"ffmpeg not found at {ffmpeg!r}") from err
    image, error = await process.communicate(video)
    if process.returncode != 0 or not image:
        raise RuntimeError(
            f"ffmpeg could not decode a frame: {error.decode(errors='replace')[:200]}"
        )
    return image


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
        self._refresh_lock = asyncio.Lock()
        self._clock: Callable[[], float] = time.monotonic
        self._last_refresh: float | None = None
        self._discovered: dict[str, CameraInfo] = {}
        self._handles: dict[str, TapoCloudCamera] = {}
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
        """Renew the session token. Raises :class:`CloudAuthError` if refused.

        Every renewal, successful or not, is timestamped, so the throttled
        variant below also counts renewals made by the polling coordinator.
        """
        self._last_refresh = self._clock()
        try:
            await self._session.refresh()
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err
        return self.tokens

    async def async_refresh_token_throttled(
        self, min_interval: float = TOKEN_REFRESH_MIN_INTERVAL
    ) -> bool:
        """Renew the token unless it was renewed in the last ``min_interval`` s.

        For media paths, where a relay refusal triggers a renewal: a relay that
        keeps refusing would otherwise renew the account token on every snapshot
        of every card — about every 30 s per card — which risks rate limiting or
        locking the account. Returns ``True`` if a renewal happened, so the
        caller retries once; ``False`` means a recent renewal did not help and
        the caller should give up. Concurrent callers are serialised, so only
        one of them renews.
        """
        async with self._refresh_lock:
            now = self._clock()
            if self._last_refresh is not None and now - self._last_refresh < min_interval:
                _LOGGER.debug(
                    "token renewed %.0fs ago; not renewing again for a media refusal",
                    now - self._last_refresh,
                )
                return False
            await self.async_refresh_token()
            return True

    # -- discovery ------------------------------------------------------

    async def async_discover(self) -> list[CameraInfo]:
        """List every camera on the account, with its capabilities.

        Capability probing is best-effort per camera: a camera that fails to
        answer still appears, so it can be shown as unavailable rather than
        vanishing from the dashboard.

        Lens things of a multi-lens camera (see :class:`CameraInfo`) are never
        probed — the cloud refuses passthrough to them — and are described
        from their parent instead, with ``relay_device_id`` and ``channel`` set.
        """
        try:
            cameras = await self._session.cameras()
        except Exception as err:  # noqa: BLE001
            raise _map_error(err) from err

        lenses = map_lenses([cam.device_id for cam in cameras])

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

        parents = [cam for cam in cameras if cam.device_id not in lenses]
        described = {
            info.device_id: info
            for info in await asyncio.gather(*(describe(cam) for cam in parents))
        }

        result: list[CameraInfo] = []
        for cam in cameras:
            if cam.device_id not in lenses:
                result.append(described[cam.device_id])
                continue
            parent_id, channel = lenses[cam.device_id]
            parent = described.get(parent_id)
            result.append(
                CameraInfo(
                    device_id=cam.device_id,
                    name=cam.name or cam.model or cam.device_id,
                    model=cam.model or (parent.model if parent else ""),
                    app_server=cam.app_server,
                    firmware=parent.firmware if parent else "",
                    # Pan/tilt and the SD card belong to the parent device; which
                    # lens the motor moves is not discoverable, so lenses offer
                    # neither rather than guess.
                    has_ptz=False,
                    has_sd_card=False,
                    relay_device_id=parent_id,
                    channel=channel,
                )
            )

        self._discovered = {info.device_id: info for info in result}
        return result

    def camera(self, info: CameraInfo) -> TapoCloudCamera:
        """A control handle for one discovered camera (one per device id)."""
        handle = self._handles.get(info.device_id)
        if handle is None or handle.info != info:
            handle = TapoCloudCamera(self, info)
            self._handles[info.device_id] = handle
        return handle

    def parent_camera(self, info: CameraInfo) -> TapoCloudCamera | None:
        """The parent camera's handle for a lens, or ``None`` if not a lens."""
        if not info.is_lens:
            return None
        parent_info = self._discovered.get(info.relay_device_id) or CameraInfo(
            device_id=info.relay_device_id,
            name=info.name,
            model=info.model,
            app_server=info.app_server,
        )
        return self.camera(parent_info)


class TapoCloudCamera:
    """One camera, or one lens of a multi-lens camera, reached over the relay."""

    def __init__(self, account: TapoCloudAccount, info: CameraInfo) -> None:
        self._account = account
        self._info = info
        # Passthrough always targets the device the cloud will answer for: a
        # lens thing's own id is refused ("doesn't belong to user's account").
        self._device = account.session.device(info.stream_device_id, name=info.name)
        self._poll_lock = asyncio.Lock()
        self._last_poll: tuple[float, dict[str, Any]] | None = None

    @property
    def info(self) -> CameraInfo:
        return self._info

    @property
    def device_id(self) -> str:
        return self._info.device_id

    # -- media ----------------------------------------------------------

    async def iter_live_ts(self, *, resolution: str = "HD") -> AsyncIterator[bytes]:
        """An async iterator of MPEG-TS chunks of the live view.

        Remote by construction: relay-negotiated, no LAN path to the camera.
        A lens streams its parent device on its own channel. If the relay
        refuses the token before any video arrives, the token is renewed once
        (at most every :data:`TOKEN_REFRESH_MIN_INTERVAL` seconds, account-wide)
        and the stream is retried; any other failure is raised as before.
        """
        for attempt in (1, 2):
            produced = False
            try:
                async for chunk in self._raw_live_ts(resolution):
                    produced = True
                    yield chunk
                return
            except Unauthorized:
                if produced or attempt == 2:
                    raise
                if not await self._account.async_refresh_token_throttled():
                    raise
                _LOGGER.debug("relay refused the token for %s; renewed", self.device_id)

    def _raw_live_ts(self, resolution: str) -> AsyncIterator[bytes]:
        return iter_live_ts(
            self._account.session,
            self._info.stream_device_id,
            channel=self._info.channel,
            resolution=resolution,
        )

    async def async_snapshot(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        seconds: float = 4.0,
        resolution: str = "HD",
        max_wait: float = 45.0,
    ) -> bytes:
        """Return one JPEG still of the live view (of this lens, for a lens).

        The camera has no still-image endpoint, so this collects about
        ``seconds`` of live MPEG-TS from the relay on this camera's channel and
        decodes one frame with ffmpeg. Video goes to ffmpeg over a pipe and the
        JPEG comes back over a pipe: no temporary files, so no blocking file I/O
        on the event loop.

        A relay token refusal renews the token once, throttled account-wide,
        and retries; any other error is raised as before.

        Raises:
            CloudAuthError: the relay refused the token, even after renewal (or
                renewal was throttled).
            CloudTransportError: the relay failed or produced no video.
            RuntimeError: ffmpeg is missing or could not decode a frame.
        """
        try:
            video = await self._collect_ts(seconds, resolution, max_wait)
        except Exception as err:  # noqa: BLE001
            mapped = _map_error(err)
            if not isinstance(mapped, CloudAuthError):
                raise mapped from err
            if not await self._account.async_refresh_token_throttled():
                raise mapped from err
            _LOGGER.debug("relay refused the token for %s; renewed", self.device_id)
            try:
                video = await self._collect_ts(seconds, resolution, max_wait)
            except Exception as retry_err:  # noqa: BLE001
                raise _map_error(retry_err) from retry_err
        # Every lens arrives as its own video stream, in lens order.
        return await decode_first_frame(
            video, ffmpeg=ffmpeg, video_stream=self._info.channel
        )

    async def _collect_ts(self, seconds: float, resolution: str, max_wait: float) -> bytes:
        """Gather ``seconds`` of live video, timed from the first chunk."""
        loop = asyncio.get_running_loop()
        chunks: list[bytes] = []

        async def gather() -> None:
            deadline: float | None = None
            stream = self._raw_live_ts(resolution)
            try:
                async for chunk in stream:
                    chunks.append(chunk)
                    if deadline is None:
                        deadline = loop.time() + seconds
                    if loop.time() >= deadline:
                        break
            finally:
                await stream.aclose()  # type: ignore[attr-defined]

        try:
            await asyncio.wait_for(gather(), timeout=max_wait)
        except TimeoutError:
            if not chunks:
                raise RelayError("relay produced no video before timing out") from None
        if not chunks:
            raise RelayError("relay produced no video")
        return b"".join(chunks)

    # -- state ----------------------------------------------------------

    async def async_poll(self, *, max_age: float = 0.0) -> dict[str, Any]:
        """Read the state this integration surfaces as entities.

        Each read is independent and best-effort: a camera model that lacks a
        feature answers ``-40101`` (``UnsupportedMethod``), which is recorded as
        ``None`` rather than failing the whole poll.

        A lens never polls on its own: it returns its parent's state, reusing a
        parent poll up to :data:`LENS_POLL_SHARE_SECONDS` old, so the parent is
        not queried once per lens. ``max_age`` lets that reuse happen.
        """
        parent = self._account.parent_camera(self._info)
        if parent is not None:
            return await parent.async_poll(max_age=LENS_POLL_SHARE_SECONDS)

        async with self._poll_lock:
            now = self._account._clock()
            if self._last_poll is not None and now - self._last_poll[0] <= max_age:
                return dict(self._last_poll[1])
            state = await self._poll_device()
            self._last_poll = (self._account._clock(), state)
            return dict(state)

    async def _poll_device(self) -> dict[str, Any]:
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
