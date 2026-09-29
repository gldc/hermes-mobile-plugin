"""Contract tests against the REAL hermes core on PYTHONPATH.

Run under both v2026.8.18 (0.20.4) and v2026.9.24 (0.21.5). The kwargs are
captured from core's own pre_tool_call dispatcher, not assumed, and pushed
through a real PluginManager. From v2026.9.24 a pre_tool_call callback that
raises or overruns plugins.hook_callback_timeout BLOCKS the tool, so "no
directive under every failure" is the contract that matters.
"""

from __future__ import annotations

import threading
import time

import pytest

import hermes_cli.lifecycle as lifecycle
import hermes_cli.plugins as core_plugins
from hermes_mobile.device_store import DeviceStore
from hermes_mobile.session_notify import SessionClaimRegistry, SessionNotifier


class RecordingPush:
    def __init__(self):
        self.sent = []

    def send(self, token, title="Hermes", body=None, data=None):
        self.sent.append({"token": token, "body": body, "data": data})
        return True


class RaisingPush:
    def send(self, *args, **kwargs):
        raise RuntimeError("expo down")


class RaisingRegistry:
    def resolve(self, *ids):
        raise RuntimeError("registry broken")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


@pytest.fixture
def claimed(tmp_path):
    store = DeviceStore(path=tmp_path / "devices.json")
    dev, _ = store.create_device("phone")
    store.set_push_token(dev, "ExponentPushToken[abc]")
    registry = SessionClaimRegistry()
    registry.claim(dev, "LIVE", "SKEY", route_id="SKEY")
    return store, registry


def _core_kwargs(monkeypatch, tool_name):
    """The exact kwargs core hands pre_tool_call callbacks for one tool call."""
    captured = {}

    def fake_invoke(hook_name, **kwargs):
        captured[hook_name] = kwargs
        return []

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "invoke_hook", fake_invoke)
        core_plugins._get_pre_tool_call_directive_details(
            tool_name,
            {"question": "Deploy to prod?"},
            task_id="SKEY",
            session_id="LIVE",
            tool_call_id="call_1",
            turn_id="turn_1",
        )
    return captured["pre_tool_call"]


def _through_core(monkeypatch, notifier, tool_name):
    pm = core_plugins.PluginManager()
    pm._hooks["pre_tool_call"] = [notifier.on_pre_tool_call]
    return pm.invoke_hook("pre_tool_call", **_core_kwargs(monkeypatch, tool_name))


def test_core_passes_tool_name_and_session_ids_to_pre_tool_call(monkeypatch):
    kw = _core_kwargs(monkeypatch, "clarify")
    assert kw["tool_name"] == "clarify"
    assert kw["session_id"] == "LIVE"
    assert kw["task_id"] == "SKEY"
    assert kw["tool_call_id"] == "call_1"


@pytest.mark.parametrize(
    "case", ["clarify-claimed", "other-tool", "push-raises", "registry-raises"]
)
def test_handler_never_yields_a_directive_through_core(monkeypatch, claimed, case):
    store, registry = claimed
    push = RaisingPush() if case == "push-raises" else RecordingPush()
    notifier = SessionNotifier(
        store=store,
        push=push,
        registry=RaisingRegistry() if case == "registry-raises" else registry,
        background=lambda fn: fn(),
    )
    tool = "terminal" if case == "other-tool" else "clarify"
    assert _through_core(monkeypatch, notifier, tool) == []
    if case == "clarify-claimed":
        assert push.sent == [
            {
                "token": "ExponentPushToken[abc]",
                "body": "Hermes has a question",
                "data": {"type": "clarify_request", "session_id": "SKEY"},
            }
        ]


def test_clarify_hook_returns_promptly_through_core_when_expo_hangs(
    monkeypatch, claimed
):
    store, registry = claimed
    release = threading.Event()

    class HangingPush:
        def send(self, *args, **kwargs):
            release.wait(10)
            return True

    notifier = SessionNotifier(store=store, push=HangingPush(), registry=registry)
    t0 = time.monotonic()
    try:
        assert _through_core(monkeypatch, notifier, "clarify") == []
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()


def test_core_accepts_every_hook_we_register():
    for hook in ("on_session_end", "pre_approval_request", "pre_tool_call"):
        assert hook in core_plugins.VALID_HOOKS
