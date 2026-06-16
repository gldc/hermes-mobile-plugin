from hermes_mobile.session_notify import SessionClaimRegistry


def test_claim_and_resolve_by_either_id():
    clock = {"t": 1000.0}
    reg = SessionClaimRegistry(ttl_seconds=100, clock=lambda: clock["t"])
    reg.claim("dev1", "SID", "SKEY")
    assert reg.resolve("SID") == "dev1"
    assert reg.resolve("SKEY") == "dev1"
    assert reg.resolve("nope") is None
    assert reg.resolve(None, "", "SID") == "dev1"  # skips falsy, finds the hit


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


import pytest
from hermes_mobile.device_store import DeviceStore
from hermes_mobile.session_notify import SessionNotifier, get_registry


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
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SKEY", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["data"] == {"type": "session_end"}
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


def test_disabled_toggle(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("MOBILE_NOTIFY_ON_SESSION_END", "0")
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False)
    assert push.sent == []


def test_approval_pushes_for_claimed_gateway_session(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="gateway")
    assert len(push.sent) == 1
    assert push.sent[0]["data"] == {"type": "approval_request"}
    assert push.sent[0]["body"] == "Hermes needs your approval"


def test_approval_skips_non_gateway_surface(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="cli")
    assert push.sent == []


def test_fan_out_skips_revoked_and_tokenless(store):
    _tokened(store, name="good")
    store.create_device("no-token")
    rid = _tokened(store, name="revoked", token="ExponentPushToken[r]")
    store.revoke(rid)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"


def test_absorbs_injected_kwargs(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    # invoke_hook injects telemetry_schema_version etc.
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False,
                     turn_id="t", completed=True, model="m", platform="tui",
                     telemetry_schema_version=1)
    assert len(push.sent) == 1
