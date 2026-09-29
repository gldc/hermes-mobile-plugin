import threading
import time

import pytest

from hermes_mobile.device_store import DeviceStore
from hermes_mobile.session_notify import (
    ClarifyPushGate,
    SessionClaimRegistry,
    SessionNotifier,
    get_registry,
)


def test_claim_and_resolve_by_either_id():
    clock = {"t": 1000.0}
    reg = SessionClaimRegistry(ttl_seconds=100, clock=lambda: clock["t"])
    reg.claim("dev1", "SID", "SKEY")
    # resolve() now returns (device_id, route_id); route_id falls back to the
    # first claimed id ("SID") when claim() is called without an explicit one.
    assert reg.resolve("SID") == ("dev1", "SID")
    assert reg.resolve("SKEY") == ("dev1", "SID")
    assert reg.resolve("nope") is None
    assert reg.resolve(None, "", "SID") == ("dev1", "SID")  # skips falsy, finds the hit


def test_ttl_eviction():
    clock = {"t": 1000.0}
    reg = SessionClaimRegistry(ttl_seconds=100, clock=lambda: clock["t"])
    reg.claim("dev1", "SID")
    clock["t"] = 1101.0  # past ttl
    assert reg.resolve("SID") is None


def test_claim_without_device_is_noop():
    reg = SessionClaimRegistry()
    reg.claim("", "SID")
    assert reg.resolve("SID") is None


class RecordingPush:
    def __init__(self):
        self.sent = []

    def send(self, token, title="Hermes", body=None, data=None):
        self.sent.append({"token": token, "title": title, "body": body, "data": data})
        return True


@pytest.fixture
def store(tmp_path):
    return DeviceStore(path=tmp_path / "devices.json")


@pytest.fixture(autouse=True)
def _clear_registry():
    get_registry()._by_id.clear()
    yield
    get_registry()._by_id.clear()


def _tokened(store, name="phone", token="ExponentPushToken[abc]"):
    device_id, _ = store.create_device(name)
    store.set_push_token(device_id, token)
    return device_id


def test_session_end_pushes_for_claimed_session(store):
    dev = _tokened(store)
    push = RecordingPush()
    get_registry().claim(dev, "SID", "SKEY", route_id="SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SKEY", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"
    assert push.sent[0]["data"] == {"type": "session_end", "session_id": "SKEY"}
    assert push.sent[0]["body"] == "Your session is ready — tap to check"


def test_session_end_skips_unclaimed_session(store):
    _tokened(store)
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="UNCLAIMED", task_id="X", interrupted=False)
    assert push.sent == []  # browser/CLI/subagent never claimed -> excluded


def test_session_end_skips_interrupted(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=True)
    assert push.sent == []


def test_session_end_pushes_for_cron(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="whatever", task_id="x", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["data"] == {"type": "session_end"}  # cron: broadcast, no id


def test_cron_dedup_when_delivered_to_mobile(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    # Patch the dedup reader directly (it reads a ContextVar via get_session_env).
    monkeypatch.setattr(
        "hermes_mobile.session_notify._already_delivered_to_mobile", lambda: True
    )
    n.on_session_end(session_id="whatever", task_id="x", interrupted=False)
    assert push.sent == []


def test_already_delivered_reads_session_context_not_os_environ(monkeypatch):
    """Real-path dedup reader: must go through gateway's get_session_env (a
    ContextVar accessor), NOT os.getenv. HERMES_CRON_AUTO_DELIVER_PLATFORM is
    set on the cron ContextVar, never in os.environ — regressing this back to
    os.getenv would always read None and silently double-notify.

    The function does a lazy ``from gateway.session_context import
    get_session_env``; patching the attribute on that module before the call is
    what the real code resolves. os.environ is deliberately left unset so the
    test fails if the body ever reads it instead.
    """
    import gateway.session_context as sc
    from hermes_mobile import session_notify

    monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", raising=False)

    monkeypatch.setattr(
        sc,
        "get_session_env",
        lambda name, default="": (
            "mobile" if name == "HERMES_CRON_AUTO_DELIVER_PLATFORM" else default
        ),
    )
    assert session_notify._already_delivered_to_mobile() is True

    # Delivered elsewhere (or nowhere) -> not deduped.
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": "slack")
    assert session_notify._already_delivered_to_mobile() is False
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": "")
    assert session_notify._already_delivered_to_mobile() is False


def test_disabled_toggle(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("MOBILE_NOTIFY_ON_SESSION_END", "0")
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False)
    assert push.sent == []


def test_approval_pushes_for_claimed_gateway_session(store):
    dev = _tokened(store)
    push = RecordingPush()
    get_registry().claim(dev, "SID", "SKEY", route_id="SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="gateway")
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"
    assert push.sent[0]["data"] == {"type": "approval_request", "session_id": "SKEY"}
    assert push.sent[0]["body"] == "Hermes needs your approval"


def test_approval_skips_non_gateway_surface(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="cli")
    assert push.sent == []


def test_fan_out_skips_revoked_and_tokenless(store, monkeypatch):
    # The revoked/tokenless skip lives on the BROADCAST (cron) path; a claimed
    # send is targeted at a single device via get_push_token instead.
    _tokened(store, name="good")
    store.create_device("no-token")
    rid = _tokened(store, name="revoked", token="ExponentPushToken[r]")
    store.revoke(rid)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="whatever", task_id="x", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"
    assert push.sent[0]["data"] == {"type": "session_end"}


def test_absorbs_injected_kwargs(store):
    dev = _tokened(store)
    push = RecordingPush()
    get_registry().claim(dev, "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    # invoke_hook injects telemetry_schema_version etc.
    n.on_session_end(
        session_id="SID",
        task_id="SID",
        interrupted=False,
        turn_id="t",
        completed=True,
        model="m",
        platform="tui",
        telemetry_schema_version=1,
    )
    assert len(push.sent) == 1


def test_registry_returns_canonical_route_id_even_when_matched_on_live_id():
    from hermes_mobile.session_notify import SessionClaimRegistry

    reg = SessionClaimRegistry()
    # App claims with BOTH ids; session_key (STORED) is the route id.
    reg.claim("dev-1", "LIVE-1", "STORED-1", route_id="STORED-1")
    # Resolving on the LIVE id still yields the STORED route id.
    assert reg.resolve("LIVE-1") == ("dev-1", "STORED-1")
    assert reg.resolve("STORED-1") == ("dev-1", "STORED-1")
    assert reg.resolve("nope") is None


def test_registry_route_id_falls_back_to_first_id_when_unspecified():
    from hermes_mobile.session_notify import SessionClaimRegistry

    reg = SessionClaimRegistry()
    reg.claim("dev-2", "ONLY-ID")
    assert reg.resolve("ONLY-ID") == ("dev-2", "ONLY-ID")


def test_session_end_emits_stored_id_and_targets_only_the_claiming_device(store):
    # Two devices, each claims a distinct session.
    dev_a = _tokened(store, name="A", token="ExponentPushToken[A]")
    dev_b = _tokened(store, name="B", token="ExponentPushToken[B]")
    push = RecordingPush()
    reg = get_registry()
    reg.claim(dev_a, "LIVE-A", "STORED-A", route_id="STORED-A")
    reg.claim(dev_b, "LIVE-B", "STORED-B", route_id="STORED-B")
    n = SessionNotifier(store=store, push=push, registry=reg)
    # The gateway hands the hook the LIVE id; we must still emit the STORED id
    # and target ONLY the claiming device.
    n.on_session_end(session_id="LIVE-A", task_id=None, interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[A]"  # only A, never B
    assert push.sent[0]["data"] == {"type": "session_end", "session_id": "STORED-A"}


# ---------------------------------------------------------------------------
# coalesced approvals (spec §9.1)
# ---------------------------------------------------------------------------


def test_coalesced_approval_follower_does_not_push(store):
    dev = _tokened(store)
    push = RecordingPush()
    get_registry().claim(dev, "SID", "SKEY", route_id="SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(
        session_key="SKEY", surface="gateway", command="rm -rf /tmp/x", coalesced=True
    )
    assert push.sent == []
    # The leader (no coalesced flag) still pushes.
    n.on_pre_approval_request(
        session_key="SKEY", surface="gateway", command="rm -rf /tmp/x"
    )
    assert len(push.sent) == 1


# ---------------------------------------------------------------------------
# clarify push via pre_tool_call (spec §6.5, §9.1)
# ---------------------------------------------------------------------------


def _sync(fn):
    fn()


def _no_thread(fn):
    raise RuntimeError("can't start new thread")


class RaisingPush:
    def send(self, *args, **kwargs):
        raise RuntimeError("expo down")


class RaisingRegistry:
    def resolve(self, *ids):
        raise RuntimeError("registry broken")


class RaisingStore:
    def get_push_token(self, device_id):
        raise RuntimeError("store broken")

    def list_devices(self):
        raise RuntimeError("store broken")


class SpyStore:
    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)


def test_clarify_pushes_redacted_to_the_claiming_device_only(store):
    dev_a = _tokened(store, name="A", token="ExponentPushToken[A]")
    _tokened(store, name="B", token="ExponentPushToken[B]")
    reg = get_registry()
    reg.claim(dev_a, "LIVE-A", "STORED-A", route_id="STORED-A")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=reg, background=_sync)
    ret = n.on_pre_tool_call(
        tool_name="clarify",
        args={"question": "Deploy to prod or staging?", "choices": ["prod", "staging"]},
        session_id="LIVE-A",
        task_id="STORED-A",
        tool_call_id="call_1",
        turn_id="turn_1",
        api_request_id="",
        middleware_trace=[],
        telemetry_schema_version=1,
    )
    assert ret is None
    assert push.sent == [
        {
            "token": "ExponentPushToken[A]",
            "title": "Hermes",
            "body": "Hermes has a question",
            "data": {"type": "clarify_request", "session_id": "STORED-A"},
        }
    ]
    assert "Deploy" not in repr(push.sent) and "staging" not in repr(push.sent)


@pytest.mark.parametrize("tool", ["terminal", "read_file", "delegate_task", "", None])
def test_non_clarify_tools_never_push_or_touch_the_store(tool):
    get_registry().claim("dev-x", "SID")
    push = RecordingPush()
    spy = SpyStore()
    n = SessionNotifier(store=spy, push=push, registry=get_registry(), background=_sync)
    assert (
        n.on_pre_tool_call(
            tool_name=tool, args={"command": "ls"}, session_id="SID", task_id="SID"
        )
        is None
    )
    assert push.sent == []
    assert spy.touched == []


def test_clarify_for_an_unclaimed_session_does_not_push(store):
    _tokened(store)
    push = RecordingPush()
    n = SessionNotifier(
        store=store, push=push, registry=get_registry(), background=_sync
    )
    assert (
        n.on_pre_tool_call(tool_name="clarify", session_id="CLI-1", task_id="CLI-1")
        is None
    )
    assert push.sent == []


@pytest.mark.parametrize("broken", ["push", "registry", "store", "background"])
def test_clarify_push_failures_never_reach_the_agent(store, broken):
    dev = _tokened(store)
    get_registry().claim(dev, "SID", "SKEY", route_id="SKEY")
    n = SessionNotifier(
        store=RaisingStore() if broken == "store" else store,
        push=RaisingPush() if broken == "push" else RecordingPush(),
        registry=RaisingRegistry() if broken == "registry" else get_registry(),
        background=_no_thread if broken == "background" else _sync,
    )
    assert (
        n.on_pre_tool_call(tool_name="clarify", session_id="SID", task_id="SKEY")
        is None
    )


def test_clarify_push_runs_off_the_hook_thread(store):
    dev = _tokened(store)
    get_registry().claim(dev, "SID", route_id="SID")
    release, sent = threading.Event(), threading.Event()

    class SlowPush:
        def send(self, token, title="Hermes", body=None, data=None):
            release.wait(5)
            sent.set()
            return True

    n = SessionNotifier(store=store, push=SlowPush(), registry=get_registry())
    t0 = time.monotonic()
    try:
        assert n.on_pre_tool_call(tool_name="clarify", session_id="SID") is None
        assert time.monotonic() - t0 < 0.5
        assert not sent.is_set()
    finally:
        release.set()
    assert sent.wait(5)


def test_clarify_cooldown_drops_repeat_questions_for_one_session(store):
    dev = _tokened(store)
    reg = get_registry()
    reg.claim(dev, "S1", route_id="S1")
    reg.claim(dev, "S2", route_id="S2")
    clock = {"t": 1000.0}
    push = RecordingPush()
    n = SessionNotifier(
        store=store,
        push=push,
        registry=reg,
        background=_sync,
        clarify_gate=ClarifyPushGate(cooldown_seconds=30, clock=lambda: clock["t"]),
    )
    # A model re-asking in a loop gets one push.
    for _ in range(5):
        n.on_pre_tool_call(tool_name="clarify", session_id="S1")
    assert len(push.sent) == 1
    # Another session has its own window.
    n.on_pre_tool_call(tool_name="clarify", session_id="S2")
    assert len(push.sent) == 2
    clock["t"] += 31
    n.on_pre_tool_call(tool_name="clarify", session_id="S1")
    assert len(push.sent) == 3


def test_clarify_respects_disable_toggle(store, monkeypatch):
    dev = _tokened(store)
    get_registry().claim(dev, "SID")
    monkeypatch.setenv("MOBILE_NOTIFY_ON_SESSION_END", "0")
    push = RecordingPush()
    n = SessionNotifier(
        store=store, push=push, registry=get_registry(), background=_sync
    )
    assert n.on_pre_tool_call(tool_name="clarify", session_id="SID") is None
    assert push.sent == []
