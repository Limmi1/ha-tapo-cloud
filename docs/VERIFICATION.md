# OnTapo review and verification

This is the review that gates the project: whether OnTapo can establish a
genuine remote live stream, and whether it is safe to depend on.

Reviewed release: **ontapo 0.1.0** (sdist, SHA-256
`077d36d8f3d05992c93617addcac01d6ebe5a18eb0a0c16cbd4033bbd8905d63`), the only
release published at the time of writing.

## Verdict

**The remote media path is real**, and it is the reason this integration can
exist. It is not LAN access, not P2P/STUN hole-punching, and not a local RTSP
stream — it is an authenticated TLS session to a TP-Link relay that the cloud
hands out per request.

**But it is alpha software built on undocumented endpoints**, and the live
verification against your own cameras has not been done by this repository's
author. See *What has and has not been verified* below — run
`scripts/verify_remote_stream.py` before you rely on any of it.

## How the remote stream actually works

Traced through the source, the path is:

1. **Account login** — `POST n-wap.i.tplinkcloud.com/api/v2/account/login`,
   signed with an app signature (`Content-MD5` + HMAC-SHA1 in
   `X-Authorization`). The gateway redirects wrong-region logins by returning
   the correct `appServerUrlV2`, which OnTapo adopts automatically. MFA is a
   two-call exchange: `getPushVC4TerminalMFA` then `checkMFACodeAndLogin`.
   Returns a session token plus a refresh token.
2. **Discovery** — `GET {app_server}/v2/things` lists the account's devices.
   Each device carries its own region-correct `app_server`.
3. **Device methods** — tunnelled through
   `POST {app_server}/v1/things/{device_id}/services-sync`.
4. **Relay negotiation** — `POST {cipc_host}/v2/relay/request` with the raw
   account token, `deviceType: SMART.IPCAMERA` and `streamType: 0`
   (`PREVIEW`). The cloud answers with `relayIp`, `relayUrl` and a short-lived
   `relayToken`.
5. **Media** — a raw TLS socket to `relayIp:443` (SNI set to the relay host),
   carrying a `multipart/mixed` body. The client writes a JSON control frame
   asking for `preview`; the device streams back parts of
   `Content-Type: video/mp2t` — MPEG-TS with H.264 video and G.711 audio.

Step 4 is the crux. **The camera's location is irrelevant**: it is the account
token, not network adjacency, that authorises the media session, and both the
camera and Home Assistant dial out to the relay. That is why cameras behind
separate routers and separate internet connections work, and why no VPN, port
forwarding or gateway hardware is needed. It is also why no camera-local
password is needed — unlike the local pytapo path.

`relay.py`'s docstring calls `PREVIEW` "P2P-negotiated; experimental", which
reads alarming, but the implementation does no P2P: `stream_preview()` uses the
same relay socket as playback and only the control frame differs. The README
records live verification of exactly this path.

## Licensing

**MIT**, which is compatible with this integration (also MIT) and with
HomeAssistant-Tapo-Control's Apache-2.0, so the cloud connection mode can be
contributed upstream without a licensing obstacle.

The package ships `mergedCA.pem` (TP-Link's public CA bundle, extracted from
the Android app) and two public app-identity constants used for request
signing. `DISCLAIMER.md` states that no private keys or TP-Link source are
redistributed, and grounds the work in the interoperability exceptions (EU
Directive 2009/24/EC Art. 6; 17 U.S.C. § 1201(f)). That matches what the code
contains.

## Dependencies

Exactly one runtime dependency: **`httpx>=0.27`**. Test-only extras are
`pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`. Requires Python 3.11+.
Home Assistant 2024.11+ already ships httpx, so the integration adds no
transitive weight.

That is an unusually small surface for something doing this much — there is no
vendored binary, no C extension and no crypto library beyond the standard one.

## Security review

Reviewed the full 1,700 lines of `src/ontapo/` by hand:

| Check | Finding |
|---|---|
| Network destinations | Only `*.tplinkcloud.com` and the relay IP the cloud itself returns. No third-party host, no telemetry, no analytics. |
| `eval` / `exec` / `pickle` / `__import__` | None. |
| Subprocess use | One call: `ffmpeg` with a fixed argument vector (no shell) to decode a snapshot frame. |
| Environment / filesystem reads | None beyond the bundled CA file and the paths the caller passes. |
| TLS | `ssl.create_default_context()` plus the bundled TP-Link CA — the private CA is *added to*, not substituted for, the system trust store. Verification is never disabled. |
| Credential handling | Password used once at login and not retained. Tokens held in memory; persisting them is the caller's choice. |
| Injection in the relay path | `_open_relay()` rejects control characters in the host, path, token, track id and terminal UUID before building the request — the relay protocol is hand-rolled HTTP, so this check matters. |
| Resource bounds | Multipart parts capped at 64 MiB; a 1 MiB line buffer; a 12-second idle timeout. |

**No backdoor, no exfiltration, no credential leakage.** The one thing to be
aware of is the inherent trust model: a saved session token grants full access
to every camera on the account until it expires. This integration stores it in
Home Assistant's config entry (`.storage`), redacts it from diagnostics, and
never logs it — but treat a Home Assistant backup containing it as you would
the account password itself.

## Risks this integration has to live with

These are real and are the reason the integration is defensive where it is.

1. **The relay region is hardcoded.** `relay.py` pins
   `CIPC_HOST = "aps1-cipc-api.i.tplinkcloud.com"` — ap-southeast-1 — with no
   parameter, even though per-device `app_server` values *are* region-correct.
   The library's author verified from a UTC+8 account. **For a user outside
   that region this is the single most likely failure**, so the integration
   exposes the relay host as an option and the verification script takes
   `--relay-host`.
2. **Setters are rejected by the cloud.** The gateway validates method names
   against a per-model schema and refuses every `set*` call with HTTP 400
   `MODEL_SCHEMA_CHECK_FAILED` *before the camera sees it*. Getters work;
   `motorMove` and `motorMoveToPreset` are whitelisted and work. So over the
   cloud: **pan/tilt/presets are writable; motion detection and privacy mode
   are readable only.** The integration exposes the readable state as sensors
   and surfaces the rejection as a clear error rather than a toggle that snaps
   back.
3. **Alpha, one release, one maintainer, two camera models.** Verified against
   a C216 and a TC65. Other models speak the same protocol but implement
   different method sets; unsupported ones raise `UnsupportedMethod`, which
   this integration records as "unknown" rather than failing a camera.
4. **It can break permanently without notice.** A TP-Link app or server change
   can end this at any time. Do not make it the only path to a camera you rely
   on for security.
5. **Terms of service.** TP-Link's ToS may prohibit third-party clients. Using
   this risks the account. That is the user's decision to make knowingly.
6. **We reuse OnTapo's private helpers.** `relay_stream.py` needs an
   open-ended byte stream, which OnTapo's public API does not offer, so it
   drives OnTapo's internal relay framing directly. The coupling is confined to
   that one module, checked at setup by `assert_ontapo_compatible()`, pinned to
   `ontapo==0.1.0`, and asserted by the test suite.

## What has and has not been verified

| Claim | Status |
|---|---|
| The relay media path is architecturally remote, not LAN or P2P | ✅ verified by source review |
| MIT licence, one dependency, no malicious code | ✅ verified by source review |
| Login, MFA, discovery, live view work against real cameras | ✅ **independently verified 2026-11-26** against a current Tapo pan/tilt camera on a separate network — 2304×1296 H.264 over a public relay, no LAN/VPN. See *Live run* below. |
| Snapshots in Home Assistant, all cameras including both lenses of a dual-lens camera | ✅ verified 2026-11-26: full-resolution frames for each camera, and each lens shows its own view (0.1.5) |
| Each lens is a separate video stream in one relay stream | ✅ verified with the 0.1.4 lens probe; see [ARCHITECTURE.md](ARCHITECTURE.md#multi-lens-cameras) |
| Pan/tilt over the cloud | ✅ commands accepted by the camera without error (0.1.7); the physical movement was not observed during the test |
| Live view in the camera pop-up (MJPEG) | ⚠️ implemented and tested against fakes; not yet watched on a real camera |
| Cloud setters are rejected | ⚠️ reported by OnTapo's author; the integration assumes it and degrades gracefully if it is wrong |
| Works from outside ap-southeast-1 | ✅ verified: the hardcoded negotiation host still returned a correct regional relay |

## Live run (2026-11-26)

Reproduced against a real account and Tapo cameras on a network separate from
the test machine. `scripts/verify_remote_stream.py` negotiated a relay session,
was handed a public relay address outside the test network, and decoded **2304×1296
H.264, 2.68 MB in 8.1 s** — genuine remote live video, no LAN access.

Two live-server deviations from OnTapo 0.1.0 surfaced and are handled in the
integration (`api.async_send_mfa_code` / `async_submit_mfa`):

1. **The MFA code-send request must re-present the password** as
   `cloudPassword`. Without it the server answers `-20615 Password format
   error` — as HTTP 200, so the status alone looks like success — and sends no
   code. OnTapo's `send_mfa_code()` omits it, so codes never arrive.
2. **The verification code must be digits only.** A grouped code such as
   `150-791` is rejected with `-20106 (value out of range)`; stripping the
   separator fixes it.

Both are good candidates to contribute back to OnTapo.

Verifying the middle rows needs a Tapo account, a camera and consent to use
them — which only you can supply. That is exactly what
`scripts/verify_remote_stream.py` is for, and why this integration was built in
phases so the first phase is cheap to abandon if the script fails.

```bash
pip install ontapo==0.1.0
python scripts/verify_remote_stream.py
```

Pick a camera on a **different network from the computer running the script**.
The script prints the relay host and IP it was given, confirms the address is
public and outside your own subnets, then decodes the captured stream and
reports the codec and resolution.
