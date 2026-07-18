"""Tests for hermes_mobile.plugin_api — the /api/plugins/mobile routes.

Mounts the router exactly like hermes' web server does
(``app.include_router(router, prefix="/api/plugins/mobile")``) and
simulates the gated auth middleware by attaching a Session (or None) to
``request.state.session``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import Session

from hermes_mobile import mailbox, plugin_api
from hermes_mobile.device_store import DeviceStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_session(user_id: str, provider: str = "mobile-device") -> Session:
    return Session(
        user_id=user_id,
        email="dev",
        display_name="dev",
        org_id="",
        provider=provider,
        expires_at=2**31,
        access_token="at",
        refresh_token="rt",
    )


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json")


@pytest.fixture
def client(tmp_path, store):
    plugin_api.configure(store=store, mailbox_dir=tmp_path / "mailbox")
    app = FastAPI()

    @app.middleware("http")
    async def attach_session(request, call_next):
        request.state.session = getattr(app.state, "test_session", None)
        return await call_next(request)

    app.include_router(plugin_api.router, prefix="/api/plugins/mobile")
    try:
        yield TestClient(app)
    finally:
        plugin_api.configure()  # reset module singletons


@pytest.fixture
def device(store):
    device_id, rt = store.create_device("phone")
    return device_id, rt


def as_device(client, device_id):
    client.app.state.test_session = make_session(f"mobile:{device_id}")
    return client


# ---------------------------------------------------------------------------
# auth gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,method",
    [("/push-token", "post"), ("/mailbox", "get"), ("/me", "get")],
)
def test_routes_403_without_session(client, path, method):
    client.app.state.test_session = None  # loopback mode
    resp = getattr(client, method)(
        f"/api/plugins/mobile{path}",
        **({"json": {"token": "t"}} if method == "post" else {}),
    )
    assert resp.status_code == 403


def test_routes_403_for_foreign_provider_session(client, device):
    device_id, _ = device
    client.app.state.test_session = make_session("github:123", provider="github")
    assert client.get("/api/plugins/mobile/me").status_code == 403


def test_routes_403_for_mobile_prefix_with_wrong_provider(client, device):
    device_id, _ = device
    # user_id looks mobile-ish but the session was minted by another provider.
    client.app.state.test_session = make_session(
        f"mobile:{device_id}", provider="basic"
    )
    assert client.get("/api/plugins/mobile/me").status_code == 403


def test_routes_403_for_malformed_device_id(client):
    client.app.state.test_session = make_session("mobile:../evil")
    assert client.get("/api/plugins/mobile/me").status_code == 403


# ---------------------------------------------------------------------------
# POST /push-token
# ---------------------------------------------------------------------------


def test_push_token_registers_for_calling_device(client, store, device):
    device_id, _ = device
    resp = as_device(client, device_id).post(
        "/api/plugins/mobile/push-token", json={"token": "ExponentPushToken[z]"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert store.get_push_token(device_id) == "ExponentPushToken[z]"


def test_push_token_empty_rejected(client, device):
    device_id, _ = device
    resp = as_device(client, device_id).post(
        "/api/plugins/mobile/push-token", json={"token": "   "}
    )
    assert resp.status_code == 400


def test_push_token_unknown_device_404(client):
    resp = as_device(client, "ffffffffffffffff").post(
        "/api/plugins/mobile/push-token", json={"token": "t"}
    )
    assert resp.status_code == 404


def test_push_token_revoked_device_404(client, store, device):
    device_id, _ = device
    store.revoke(device_id)
    resp = as_device(client, device_id).post(
        "/api/plugins/mobile/push-token", json={"token": "t"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /session-claim
# ---------------------------------------------------------------------------


def test_session_claim_records_device(client, device):
    from hermes_mobile.session_notify import get_registry

    get_registry()._by_id.clear()
    device_id, _ = device
    resp = as_device(client, device_id).post(
        "/api/plugins/mobile/session-claim",
        json={"session_id": "SID", "session_key": "SKEY"},
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert get_registry().resolve("SID") == (device_id, "SKEY")
    assert get_registry().resolve("SKEY") == (device_id, "SKEY")
    get_registry()._by_id.clear()


def test_session_claim_403_without_session(client):
    client.app.state.test_session = None  # loopback mode
    resp = client.post(
        "/api/plugins/mobile/session-claim",
        json={"session_id": "SID", "session_key": "SKEY"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /mailbox
# ---------------------------------------------------------------------------


def test_mailbox_returns_and_drains(client, tmp_path, device):
    device_id, _ = device
    mailbox.append_message(tmp_path / "mailbox", device_id, "one")
    mailbox.append_message(tmp_path / "mailbox", device_id, "two")

    c = as_device(client, device_id)
    resp = c.get("/api/plugins/mobile/mailbox")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["content"] for m in msgs] == ["one", "two"]
    assert all(m["chat_id"] == device_id for m in msgs)

    # Drained on read.
    assert c.get("/api/plugins/mobile/mailbox").json() == {"messages": []}


def test_mailbox_empty_for_new_device(client, device):
    device_id, _ = device
    resp = as_device(client, device_id).get("/api/plugins/mobile/mailbox")
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


def test_mailbox_only_sees_own_device(client, tmp_path, store, device):
    device_id, _ = device
    other_id, _ = store.create_device("other")
    mailbox.append_message(tmp_path / "mailbox", other_id, "private")
    resp = as_device(client, device_id).get("/api/plugins/mobile/mailbox")
    assert resp.json() == {"messages": []}
    # Other device's mail is untouched.
    assert (tmp_path / "mailbox" / f"{other_id}.jsonl").exists()


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


def test_me_returns_device_info_without_secrets(client, store, device):
    device_id, _ = device
    store.set_push_token(device_id, "tok")
    resp = as_device(client, device_id).get("/api/plugins/mobile/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == device_id
    assert body["name"] == "phone"
    assert body["revoked"] is False
    assert body["has_push_token"] is True
    text = resp.text
    assert "hash" not in text and "push_token" not in text.replace("has_push_token", "")


def test_me_unknown_device_404(client):
    resp = as_device(client, "ffffffffffffffff").get("/api/plugins/mobile/me")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# dashboard shim + manifest
# ---------------------------------------------------------------------------


def test_dashboard_shim_exports_router_like_web_server_loads_it():
    """Import dashboard/plugin_api.py exactly like _mount_plugin_api_routes."""
    api_path = REPO_ROOT / "dashboard" / "plugin_api.py"
    module_name = "hermes_dashboard_plugin_mobile"
    spec = importlib.util.spec_from_file_location(module_name, api_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
        assert getattr(mod, "router", None) is plugin_api.router
    finally:
        sys.modules.pop(module_name, None)


def test_dashboard_manifest_declares_api():
    import json

    manifest = json.loads((REPO_ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "mobile"
    assert manifest["api"] == "plugin_api.py"
    assert (REPO_ROOT / "dashboard" / manifest["api"]).exists()
    assert manifest["tab"].get("hidden") is True
