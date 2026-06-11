"""Tests for hermes_mobile.cli — the `hermes mobile` subcommand."""

from __future__ import annotations

import argparse
import io
import json
import sys
import types

import pytest

from hermes_mobile import cli
from hermes_mobile.device_store import DeviceStore


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json")


def _run(argv, store, monkeypatch=None):
    """Build the parser exactly like hermes does, parse argv, dispatch."""
    parser = argparse.ArgumentParser(prog="hermes mobile")
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    out = io.StringIO()
    rc = cli.handle(args, store=store, out=out)
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# registration surface
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self):
        self.cli_commands = []

    def register_cli_command(
        self, name, help, setup_fn, handler_fn=None, description=""
    ):
        self.cli_commands.append({
            "name": name,
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        })


def test_register_cli_registers_mobile_command(store):
    ctx = FakeCtx()
    cli.register_cli(ctx, store)
    assert len(ctx.cli_commands) == 1
    cmd = ctx.cli_commands[0]
    assert cmd["name"] == "mobile"
    assert cmd["help"]
    assert callable(cmd["setup_fn"])
    assert callable(cmd["handler_fn"])


def test_registered_handler_round_trip(store, monkeypatch, capsys):
    """Drive the registered setup_fn/handler_fn the way hermes' main() does."""
    ctx = FakeCtx()
    cli.register_cli(ctx, store)
    cmd = ctx.cli_commands[0]
    parser = argparse.ArgumentParser(prog="hermes mobile")
    cmd["setup_fn"](parser)
    parser.set_defaults(func=cmd["handler_fn"])
    args = parser.parse_args(["pair", "--name", "phone", "--url", "http://gw:9119"])
    rc = args.func(args)
    assert rc == 0
    assert len(store.list_devices()) == 1
    capsys.readouterr()  # handler_fn writes to real stdout here; just drain


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------


def test_pair_prints_payload_json_and_warning(store):
    rc, out = _run(
        ["pair", "--name", "my-phone", "--url", "http://100.1.2.3:9119"], store
    )
    assert rc == 0
    assert "LIVE refresh token" in out
    payload_line = next(line for line in out.splitlines() if line.startswith("{"))
    payload = json.loads(payload_line)
    assert payload["url"] == "http://100.1.2.3:9119"
    assert set(payload) == {"url", "rt", "device_id"}

    # The minted RT must actually authenticate against the store.
    at, new_rt, exp = store.rotate_refresh(payload["rt"])
    assert at and new_rt
    devices = store.list_devices()
    assert devices[0]["device_id"] == payload["device_id"]
    assert devices[0]["name"] == "my-phone"


def test_pair_default_name_and_detected_url(store, monkeypatch):
    monkeypatch.setattr(cli, "detect_gateway_url", lambda: "http://100.9.9.9:9119")
    rc, out = _run(["pair"], store)
    assert rc == 0
    payload = json.loads(next(l for l in out.splitlines() if l.startswith("{")))
    assert payload["url"] == "http://100.9.9.9:9119"
    assert store.list_devices()[0]["name"].startswith("device-")


def test_pair_without_qrcode_package_prints_manual_note(store, monkeypatch):
    monkeypatch.setitem(sys.modules, "qrcode", None)  # import qrcode -> ImportError
    rc, out = _run(["pair", "--url", "http://x:9119"], store)
    assert rc == 0
    assert "qrcode" in out
    assert "manually" in out


def test_pair_with_qrcode_package_renders_qr(store, monkeypatch):
    rendered = {}

    class FakeQR:
        def __init__(self, border=4):
            self.data = ""

        def add_data(self, data):
            self.data = data

        def print_ascii(self, out=None, invert=False):
            rendered["payload"] = self.data
            print("##FAKE-QR##", file=out)

    fake_mod = types.ModuleType("qrcode")
    fake_mod.QRCode = FakeQR
    monkeypatch.setitem(sys.modules, "qrcode", fake_mod)

    rc, out = _run(["pair", "--url", "http://x:9119"], store)
    assert rc == 0
    assert "##FAKE-QR##" in out
    # QR encodes exactly the printed JSON payload.
    payload_line = next(l for l in out.splitlines() if l.startswith("{"))
    assert rendered["payload"] == payload_line


# ---------------------------------------------------------------------------
# devices / revoke
# ---------------------------------------------------------------------------


def test_devices_empty(store):
    rc, out = _run(["devices"], store)
    assert rc == 0
    assert "No paired devices" in out


def test_devices_lists_and_marks_revoked(store):
    d1, _ = store.create_device("alpha")
    d2, _ = store.create_device("beta")
    store.revoke(d2)
    rc, out = _run(["devices"], store)
    assert rc == 0
    assert d1 in out and d2 in out
    assert "alpha" in out and "beta" in out
    assert "revoked" in out and "active" in out
    # No token material in the listing.
    assert "refresh_token" not in out


def test_revoke_known_device(store):
    device_id, rt = store.create_device("phone")
    rc, out = _run(["revoke", device_id], store)
    assert rc == 0
    assert device_id in out
    with pytest.raises(Exception):
        store.rotate_refresh(rt)


def test_revoke_unknown_device(store):
    rc, out = _run(["revoke", "nope"], store)
    assert rc == 1
    assert "Unknown device" in out


def test_no_subcommand_prints_help(store):
    rc, out = _run([], store)
    assert rc == 1
    assert "pair" in out and "revoke" in out


# ---------------------------------------------------------------------------
# URL detection helpers
# ---------------------------------------------------------------------------


def test_detect_gateway_url_prefers_tailscale(monkeypatch):
    def fake_probe(addr):
        return "100.74.3.2" if addr == "100.100.100.100" else "192.168.1.5"

    monkeypatch.setattr(cli, "_probe_local_ip", fake_probe)
    assert cli.detect_gateway_url() == "http://100.74.3.2:9119"


def test_detect_gateway_url_falls_back_to_lan(monkeypatch):
    def fake_probe(addr):
        # Tailscale probe routes out the default NIC -> non-CGNAT address.
        return "192.168.1.5"

    monkeypatch.setattr(cli, "_probe_local_ip", fake_probe)
    assert cli.detect_gateway_url() == "http://192.168.1.5:9119"


def test_detect_gateway_url_loopback_last_resort(monkeypatch):
    monkeypatch.setattr(cli, "_probe_local_ip", lambda addr: None)
    assert cli.detect_gateway_url() == "http://127.0.0.1:9119"
