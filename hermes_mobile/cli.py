"""`hermes mobile` CLI — pairing and device management.

Registered through ``ctx.register_cli_command`` (CONTRACTS.md §1.2):
*setup_fn* receives an argparse subparser, *handler_fn* is dispatched by
hermes' main parser as ``args.func(args)``.

Subcommands:

* ``hermes mobile pair [--name NAME] [--url URL]`` — mints a device via
  the :class:`~hermes_mobile.device_store.DeviceStore` and prints the
  pairing payload JSON ``{"url", "rt", "device_id"}`` plus a terminal QR
  of that JSON when the optional ``qrcode`` package is importable. The
  payload contains a live refresh token — output warns loudly.
* ``hermes mobile devices`` — list paired devices.
* ``hermes mobile revoke <device_id>`` — revoke a device's tokens.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from typing import Optional

from .device_store import DeviceStore

#: Default dashboard port for the pairing URL when --url is not given.
DEFAULT_GATEWAY_PORT = 9119

SECRET_WARNING = (
    "WARNING: the pairing payload below contains a LIVE refresh token (a "
    "secret). Anyone who scans the QR or copies the JSON can authenticate "
    "as this device until you run `hermes mobile revoke`."
)

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # CGNAT range Tailscale uses


# ---------------------------------------------------------------------------
# Gateway URL detection (best-effort, injectable for tests)
# ---------------------------------------------------------------------------


def _probe_local_ip(probe_addr: str) -> Optional[str]:
    """Source IP the OS would use to reach *probe_addr* (no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe_addr, 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def detect_local_ip() -> str:
    """Best-effort local IP: prefer the Tailscale address, then LAN, then loopback."""
    # Routing towards Tailscale's magic-DNS address reveals our tailnet IP
    # when Tailscale is up (the kernel picks the tailscale0 source address).
    ip = _probe_local_ip("100.100.100.100")
    if ip:
        try:
            if ipaddress.ip_address(ip) in _TAILSCALE_NET:
                return ip
        except ValueError:
            pass
    ip = _probe_local_ip("8.8.8.8")
    if ip and not ip.startswith("127."):
        return ip
    return "127.0.0.1"


def detect_gateway_url() -> str:
    return f"http://{detect_local_ip()}:{DEFAULT_GATEWAY_PORT}"


# ---------------------------------------------------------------------------
# QR rendering (optional dependency)
# ---------------------------------------------------------------------------


def _print_qr(payload: str, out) -> bool:
    """Render *payload* as a terminal QR. Returns False if qrcode is missing."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.print_ascii(out=out, invert=True)
        return True
    except Exception as exc:  # never let cosmetic QR failures kill pairing
        print(f"(QR rendering failed: {exc})", file=out)
        return False


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_pair(args: argparse.Namespace, store: DeviceStore, out=None) -> int:
    out = out or sys.stdout
    name = args.name or f"device-{time.strftime('%Y%m%d-%H%M%S')}"
    device_id, refresh_token = store.create_device(name)
    url = args.url or detect_gateway_url()
    payload = json.dumps(
        {"url": url, "rt": refresh_token, "device_id": device_id},
        separators=(",", ":"),
    )

    print(f"Paired new device '{name}' ({device_id})", file=out)
    print("", file=out)
    print(SECRET_WARNING, file=out)
    print("", file=out)
    print(payload, file=out)
    print("", file=out)
    if not _print_qr(payload, out):
        print(
            "QR rendering unavailable (python package 'qrcode' is not "
            "installed — `pip install qrcode`). Copy the JSON payload above "
            "into the mobile app manually.",
            file=out,
        )
    else:
        print("Scan the QR with the Hermes mobile app to finish pairing.", file=out)
    return 0


def cmd_devices(args: argparse.Namespace, store: DeviceStore, out=None) -> int:
    out = out or sys.stdout
    devices = store.list_devices()
    if not devices:
        print("No paired devices. Run `hermes mobile pair` to add one.", file=out)
        return 0
    header = (
        f"{'DEVICE ID':<18} {'NAME':<24} {'STATUS':<8} {'CREATED':<20} LAST REFRESH"
    )
    print(header, file=out)
    for dev in sorted(devices, key=lambda d: d.get("created_at", 0)):
        status = "revoked" if dev.get("revoked") else "active"
        created = _fmt_ts(dev.get("created_at", 0))
        last = _fmt_ts(dev.get("last_refresh_at", 0)) or "never"
        print(
            f"{dev.get('device_id', '?'):<18} {str(dev.get('name', '')):<24} "
            f"{status:<8} {created:<20} {last}",
            file=out,
        )
    return 0


def cmd_revoke(args: argparse.Namespace, store: DeviceStore, out=None) -> int:
    out = out or sys.stdout
    device_id = args.device_id
    known = {d.get("device_id") for d in store.list_devices()}
    if device_id not in known:
        print(f"Unknown device id: {device_id}", file=out)
        return 1
    store.revoke(device_id)
    print(f"Device {device_id} revoked. Its tokens no longer authenticate.", file=out)
    return 0


def _fmt_ts(ts) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def setup_parser(parser: argparse.ArgumentParser) -> None:
    """Populate the `hermes mobile` subparser (ctx.register_cli_command setup_fn)."""
    sub = parser.add_subparsers(dest="mobile_cmd")

    pair = sub.add_parser(
        "pair",
        help="Mint a device token and print pairing payload + QR",
    )
    pair.add_argument("--name", default=None, help="Human-readable device name")
    pair.add_argument(
        "--url",
        default=None,
        help=(
            "Gateway URL embedded in the pairing payload "
            f"(default: http://<detected tailscale/LAN ip>:{DEFAULT_GATEWAY_PORT})"
        ),
    )

    sub.add_parser("devices", help="List paired mobile devices")

    revoke = sub.add_parser("revoke", help="Revoke a paired device's tokens")
    revoke.add_argument("device_id", help="Device id (see `hermes mobile devices`)")

    # Keep a handle for "no subcommand" help output.
    parser.set_defaults(_mobile_parser=parser)


def handle(
    args: argparse.Namespace, store: Optional[DeviceStore] = None, out=None
) -> int:
    """Dispatch `hermes mobile <cmd>` (ctx.register_cli_command handler_fn)."""
    out = out or sys.stdout
    if store is None:
        store = DeviceStore()
    cmd = getattr(args, "mobile_cmd", None)
    if cmd == "pair":
        return cmd_pair(args, store, out=out)
    if cmd == "devices":
        return cmd_devices(args, store, out=out)
    if cmd == "revoke":
        return cmd_revoke(args, store, out=out)
    parser = getattr(args, "_mobile_parser", None)
    if parser is not None:
        parser.print_help(file=out)
    else:
        print("usage: hermes mobile {pair,devices,revoke}", file=out)
    return 1


def register_cli(ctx, store: DeviceStore) -> None:
    """Register the `hermes mobile` subcommand on the plugin context."""
    ctx.register_cli_command(
        name="mobile",
        help="Pair and manage Hermes mobile devices",
        setup_fn=setup_parser,
        handler_fn=lambda args: handle(args, store=store),
        description=(
            "Pair mobile devices with this gateway over QR "
            "(`hermes mobile pair`), list them (`devices`), and revoke "
            "their tokens (`revoke <device_id>`)."
        ),
    )
