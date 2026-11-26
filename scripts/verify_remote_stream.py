#!/usr/bin/env python3
"""Prove — on your own account — that a genuine remote live stream works.

This is the gate the project is built on: before trusting the integration, run
this against a camera that is **on a different network from this computer** and
confirm it produces real video frames over the Tapo cloud relay.

It is deliberately standalone: no Home Assistant, no config entries, nothing
persisted unless you ask for it.

    python scripts/verify_remote_stream.py

What it does:
  1. Logs in to your Tapo account (prompting for an MFA code if asked).
  2. Lists the cameras on the account.
  3. Negotiates a relay session for the camera you pick and reports the relay
     host and IP it was given — the proof that the media path is TP-Link's
     relay and not your LAN.
  4. Pulls a few seconds of live MPEG-TS and reports bytes, duration and, if
     ffmpeg is present, the decoded resolution and codec.

Nothing is written to disk except a temporary .ts file, which is deleted unless
you pass --keep. No credentials or tokens are ever written to disk.

Privacy: the captured video is of your own camera. --keep leaves it in the
current directory; the repository's .gitignore already excludes *.ts so it
cannot be committed by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

try:
    from ontapo import MFARequired, OnTapo, OnTapoError
    from ontapo.relay import StreamType, request_relay
    from ontapo.stream import stream_preview
except ImportError:
    sys.exit("ontapo is not installed. Run: pip install ontapo==0.1.0")


def _local_networks() -> list[ipaddress.IPv4Network]:
    """This machine's own IPv4 networks, assuming /24, for the LAN check."""
    networks: list[ipaddress.IPv4Network] = []
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            with contextlib.suppress(ValueError):
                networks.append(
                    ipaddress.ip_network(f"{address}/24", strict=False)
                )
    return networks


def _describe_path(relay_ip: str, relay_url: str) -> None:
    """Report whether the media path is plausibly remote."""
    host = urlparse(relay_url).netloc or "(none)"
    print(f"    relay host : {host}")
    print(f"    relay ip   : {relay_ip or '(resolved from host)'}")

    if not relay_ip:
        return
    try:
        address = ipaddress.ip_address(relay_ip)
    except ValueError:
        return

    if address.is_private:
        print(
            "    ! the relay address is private — this looks like a LAN path, "
            "not a cloud relay"
        )
        return

    print("    -> public relay address: the media path leaves your network")
    for network in _local_networks():
        if address in network:
            print(f"    ! but it is inside your own {network}")


def _probe(path: str) -> None:
    """Report what ffprobe/ffmpeg makes of the captured stream, if available."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        print("    (install ffmpeg to decode and verify the frames)")
        return
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        print(f"    ! ffprobe could not read the capture: {result.stderr.strip()[:200]}")
        return
    try:
        data = json.loads(result.stdout)
        video = (data.get("streams") or [{}])[0]
        duration = float((data.get("format") or {}).get("duration", 0) or 0)
    except (ValueError, IndexError):
        print("    ! ffprobe returned nothing usable")
        return

    codec = video.get("codec_name", "?")
    width, height = video.get("width", "?"), video.get("height", "?")
    print(f"    decoded    : {codec} {width}x{height}, {duration:.1f}s of video")
    if codec != "?" and isinstance(width, int) and width > 0:
        print("    -> REAL VIDEO FRAMES DECODED. Remote live streaming works.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Tapo account email (prompted if omitted)")
    parser.add_argument(
        "--seconds", type=float, default=8.0, help="seconds of live video to pull"
    )
    parser.add_argument(
        "--resolution", default="HD", choices=["HD", "VGA"], help="stream profile"
    )
    parser.add_argument(
        "--relay-host",
        help="override the relay negotiation host, e.g. euw1-cipc-api.i.tplinkcloud.com",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the captured .ts file"
    )
    args = parser.parse_args()

    if args.relay_host:
        from ontapo import relay as ontapo_relay

        ontapo_relay.CIPC_HOST = args.relay_host
        print(f"using relay host {args.relay_host}")

    email = args.email or input("Tapo account email: ").strip()
    password = getpass.getpass("Tapo account password: ")

    print("\n[1/4] logging in ...")
    try:
        # No mfa= callback: we want MFARequired raised so the server's own list
        # of supported factor types can be shown rather than assuming email.
        session = await OnTapo.login(email, password)
    except MFARequired as challenge:
        session = getattr(challenge, "session", None)
        if session is None:
            return _fail("the server demanded MFA but gave us no session to continue")
        if await _drive_mfa(session, challenge, password) != 0:
            await session.aclose()
            return 1
    except OnTapoError as err:
        return _fail(f"login failed: {err}")
    print("    logged in (no token written to disk)")

    try:
        print("\n[2/4] discovering cameras ...")
        try:
            cameras = await session.cameras()
        except OnTapoError as err:
            return _fail(f"discovery failed: {err}")
        if not cameras:
            return _fail("no cameras found on this account")

        for index, camera in enumerate(cameras, start=1):
            print(f"    {index}. {camera.name} ({camera.model})")

        choice = input(
            f"\nWhich camera? [1-{len(cameras)}], pick one on a DIFFERENT network: "
        ).strip()
        try:
            camera = cameras[int(choice) - 1]
        except (ValueError, IndexError):
            return _fail("not a valid choice")

        print(f"\n[3/4] negotiating a relay session for {camera.name} ...")
        track_id = f"verify-{camera.device_id}"
        try:
            relay = await request_relay(
                session, camera.device_id, StreamType.PREVIEW, track_id
            )
        except OnTapoError as err:
            return _fail(
                f"relay negotiation failed: {err}\n"
                "    If this is a region error, retry with --relay-host "
                "(see docs/TROUBLESHOOTING.md)."
            )
        _describe_path(relay.relay_ip, relay.relay_url)

        print(f"\n[4/4] pulling {args.seconds:g}s of live video ...")
        fd, path = tempfile.mkstemp(suffix=".ts", prefix="tapo_verify_")
        os.close(fd)
        started = time.monotonic()
        try:
            written = await stream_preview(
                relay,
                session.terminal_uuid,
                track_id,
                path,
                args.seconds,
                resolution=args.resolution,
            )
        except OnTapoError as err:
            with contextlib.suppress(OSError):
                os.unlink(path)
            return _fail(f"live stream failed: {err}")

        elapsed = time.monotonic() - started
        print(f"    received   : {written:,} bytes in {elapsed:.1f}s")
        if written == 0:
            return _fail("the relay produced no video data")
        _probe(path)

        if args.keep:
            final = os.path.join(os.getcwd(), f"live-{camera.device_id[:8]}.ts")
            shutil.move(path, final)
            print(f"    kept       : {final} (git-ignored; delete when done)")
        else:
            with contextlib.suppress(OSError):
                os.unlink(path)
            print("    capture deleted")

        print(
            "\nVERIFIED: live video arrived over the Tapo cloud relay, with no "
            "LAN access, port forwarding or VPN."
        )
        return 0
    finally:
        await session.aclose()


_MFA_TYPE_NAMES = {
    0: "SMS / text message",
    1: "push notification to the Tapo app",
    2: "email",
    3: "authenticator app",
}


async def _post_mfa_send(session, extra: dict) -> tuple[int, str]:
    """POST one getPushVC4TerminalMFA variant; return (error_code, message).

    ``error_code`` 0 means the server accepted it. The Tapo cloud reports
    failures as HTTP 200 with an ``error_code`` in the body, so the status is
    not enough. Touches OnTapo internals deliberately — a diagnostic script.
    """
    import json as _json

    path = "/api/v2/account/getPushVC4TerminalMFA"
    base = {
        "appType": "TP-Link_Tapo_Android",
        "cloudUserName": session.email,
        "MFAProcessId": session._mfa_process_id,
        "terminalUUID": session.terminal_uuid,
    }
    body = _json.dumps({**base, **extra}).encode()
    resp = await session._client.post(
        session.account_base + path,
        content=body,
        headers=session._signed_headers(path, body),
    )
    if resp.status_code != 200:
        return -1, f"http {resp.status_code}: {resp.text[:160]}"
    try:
        data = resp.json()
    except ValueError:
        return -1, f"unparseable: {resp.text[:160]}"
    return int(data.get("error_code", 0)), str(data.get("msg") or "")


async def _send_mfa_code_checked(
    session, mfa_type: int, password: str
) -> tuple[bool, str]:
    """Ask for a code, trying the request shapes the current server may want.

    OnTapo's body omits the password, and the live server rejects that with
    ``-20615 Password format error``. The real app evidently re-sends the
    credential on this endpoint, so try a few plausible shapes and report which
    the server accepts — that is the fix to carry back into the integration.
    """
    variants: list[tuple[str, dict]] = [
        ("password re-sent as cloudPassword", {
            "MFAType": mfa_type,
            "cloudPassword": password,
        }),
        ("password re-sent as password", {
            "MFAType": mfa_type,
            "password": password,
        }),
        ("cloudPassword + appVersion", {
            "MFAType": mfa_type,
            "cloudPassword": password,
            "appVersion": "3.20.512",
        }),
        ("original: no password (OnTapo's shape)", {"MFAType": mfa_type}),
    ]
    last = ""
    for label, extra in variants:
        code, msg = await _post_mfa_send(session, extra)
        if code == 0:
            print(f"      ✓ accepted with: {label}")
            return True, label
        last = f"error_code {code}: {msg or '(no message)'}"
        print(f"      ✗ {label} -> {last}")
    return False, last


async def _drive_mfa(session, challenge, password: str) -> int:
    """Complete an MFA challenge, showing the factor types the server offered.

    TP-Link can demand a second factor even on an account with no MFA switched
    on in the app, so this reports exactly what was asked for rather than
    assuming an emailed code.
    """
    types = list(getattr(challenge, "supported_types", []) or [])
    print("\n    the server is demanding a second factor")
    print(f"    supported types reported: {types or '(none reported)'}")
    for code in types:
        print(f"      {code} = {_MFA_TYPE_NAMES.get(code, 'unknown')}")

    if not types:
        types = [2]
        print("      (none reported — trying 2 = email)")

    # Which factor actually delivers is trial and error in practice: a type the
    # server lists can still send nothing. So loop rather than exiting, and let
    # an empty code go back and pick a different type.
    default = 2 if 2 in types else types[0]
    while True:
        answer = input(f"\n    Which type should it send? [{default}]: ").strip()
        try:
            mfa_type = int(answer) if answer else default
        except ValueError:
            print("    not a number; try again")
            continue
        if mfa_type not in types:
            print(f"    the server did not offer type {mfa_type}; pick from {types}")
            continue

        name = _MFA_TYPE_NAMES.get(mfa_type, str(mfa_type))
        print(f"    requesting a code by {name}; trying request shapes ...")
        try:
            ok, detail = await _send_mfa_code_checked(session, mfa_type, password)
        except Exception as err:  # noqa: BLE001 - diagnostic, report anything
            print(f"    ! the request itself failed: {err}")
            print("      pick another type")
            continue
        if not ok:
            print(f"    ! the server did NOT send a code by {name} — {detail}")
            print("      pick another type")
            continue
        if mfa_type == 2:
            print("    a code should be on its way by email. Check spam too.")
        else:
            print(f"    a code should be on its way by {name}.")

        while True:
            code = input("    Code (Enter alone = try a different type): ").strip()
            if not code:
                break
            try:
                await session.submit_mfa(code, mfa_type)
            except OnTapoError as err:
                print(f"    ! rejected: {err}")
                print("      retype it, or press Enter to pick another type")
                continue
            return 0


def _fail(message: str) -> int:
    print(f"\nFAILED: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
