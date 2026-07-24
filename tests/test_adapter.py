"""Tests for hermes_mobile.adapter — the 'mobile' platform adapter.

Requires hermes' gateway package on PYTHONPATH (it is, per the repo test
command). ``Platform("mobile")`` only resolves once the platform name is
known to the registry, mirroring production where ``ctx.register_platform``
runs before the adapter factory — so the fixture registers a real
PlatformEntry first.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import BasePlatformAdapter, SendResult

from hermes_mobile import adapter as adapter_mod
from hermes_mobile.adapter import MobileAdapter, check_requirements, register_platform
from hermes_mobile.device_store import DeviceStore


class RecordingPush:
    def __init__(self):
        self.sent = []

    def send(self, token, title="Hermes", body=None):
        self.sent.append({"token": token, "title": title, "body": body})
        return True


@pytest.fixture(autouse=True)
def mobile_platform_registered():
    """Make Platform('mobile') resolvable, like ctx.register_platform does."""
    if not platform_registry.is_registered("mobile"):
        platform_registry.register(
            PlatformEntry(
                name="mobile",
                label="Mobile",
                adapter_factory=lambda cfg: None,
                check_fn=lambda: True,
            )
        )
        yield
        platform_registry.unregister("mobile")
    else:
        yield


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json")


@pytest.fixture
def push() -> RecordingPush:
    return RecordingPush()


@pytest.fixture
def mobile(tmp_path, store, push) -> MobileAdapter:
    return MobileAdapter(
        PlatformConfig(),
        store=store,
        push=push,
        mailbox_dir=tmp_path / "mailbox",
    )


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_connect_true_disconnect_noop(mobile):
    assert run(mobile.connect()) is True
    assert run(mobile.disconnect()) is None


def test_connect_accepts_is_reconnect(mobile):
    """The gateway always passes ``is_reconnect=`` — cold boot and watcher alike.

    ``GatewayService._connect_adapter_with_timeout`` calls
    ``adapter.connect(is_reconnect=...)`` unconditionally (gateway/run.py),
    so an adapter that omits the kwarg raises TypeError and is never added to
    ``self.adapters`` — the platform then retries forever with backoff and
    every agent→device message is dropped.
    """
    assert run(mobile.connect(is_reconnect=False)) is True
    assert run(mobile.connect(is_reconnect=True)) is True


def test_connect_signature_matches_core_contract(mobile):
    """Guard against future drift in BasePlatformAdapter.connect's kwargs."""
    core_kwonly = {
        name
        for name, p in inspect.signature(BasePlatformAdapter.connect).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    ours = inspect.signature(MobileAdapter.connect).parameters
    missing = {
        name
        for name in core_kwonly
        if name not in ours
        and not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ours.values())
    }
    assert not missing, f"MobileAdapter.connect() is missing core kwargs: {missing}"


def test_platform_identity(mobile):
    assert mobile.platform.value == "mobile"


def test_check_requirements():
    assert check_requirements() is True


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_appends_to_per_device_mailbox(tmp_path, mobile, store):
    device_id, _ = store.create_device("phone")
    result = run(mobile.send(device_id, "hello *world*"))
    assert isinstance(result, SendResult)
    assert result.success is True
    assert result.message_id

    path = tmp_path / "mailbox" / f"{device_id}.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["chat_id"] == device_id
    assert record["content"] == "hello *world*"
    assert record["ts"] > 0
    assert record["message_id"] == result.message_id


def test_send_no_push_without_token(mobile, store, push):
    device_id, _ = store.create_device("phone")
    run(mobile.send(device_id, "msg"))
    assert push.sent == []


def test_send_fires_redacted_push_when_token_registered(mobile, store, push):
    device_id, _ = store.create_device("phone")
    store.set_push_token(device_id, "ExponentPushToken[abc]")
    run(mobile.send(device_id, "secret content"))
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"
    # Redacted: message content must NOT ride in the push payload.
    assert push.sent[0]["body"] is None
    assert "secret content" not in json.dumps(push.sent[0])


def test_send_rejects_unsafe_chat_id(mobile, tmp_path):
    result = run(mobile.send("../../etc/cron.d/evil", "x"))
    assert result.success is False
    assert "invalid" in (result.error or "")
    assert not (tmp_path / "mailbox").exists()


def test_send_mailbox_io_failure_returns_retryable_error(tmp_path, store, push):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    a = MobileAdapter(PlatformConfig(), store=store, push=push, mailbox_dir=blocked)
    device_id, _ = store.create_device("phone")
    result = run(a.send(device_id, "x"))
    assert result.success is False
    assert result.retryable is True
    assert push.sent == []


def test_send_push_lookup_failure_does_not_break_delivery(tmp_path, push):
    class BrokenStore:
        def get_push_token(self, device_id):
            raise RuntimeError("store down")

        def get_device(self, device_id):
            raise RuntimeError("store down")

    a = MobileAdapter(
        PlatformConfig(),
        store=BrokenStore(),
        push=push,
        mailbox_dir=tmp_path / "mailbox",
    )
    result = run(a.send("abcd1234", "x"))
    assert result.success is True
    assert push.sent == []


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------


def test_get_chat_info_uses_device_name(mobile, store):
    device_id, _ = store.create_device("gianluca-iphone")
    info = run(mobile.get_chat_info(device_id))
    assert info == {"name": "gianluca-iphone", "type": "dm"}


def test_get_chat_info_unknown_device_falls_back_to_id(mobile):
    info = run(mobile.get_chat_info("ffffffffffffffff"))
    assert info == {"name": "ffffffffffffffff", "type": "dm"}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_register_platform_call_shape(store):
    calls = {}

    class FakeCtx:
        def register_platform(self, **kwargs):
            calls.update(kwargs)

    register_platform(FakeCtx(), store)
    assert calls["name"] == "mobile"
    assert calls["label"] == "Mobile"
    assert callable(calls["adapter_factory"])
    assert calls["check_fn"]() is True
    assert calls["platform_hint"]
    # Cron / scheduled delivery: the gateway scheduler only treats a plugin
    # platform as a delivery target when it declares a home-channel env var
    # (cron.scheduler._plugin_cron_env_var). MOBILE_HOME_CHANNEL=<device_id>
    # selects the default device for `deliver=mobile` cron jobs.
    assert calls["cron_deliver_env_var"] == "MOBILE_HOME_CHANNEL"

    built = calls["adapter_factory"](PlatformConfig())
    assert isinstance(built, MobileAdapter)
    assert built._store is store
