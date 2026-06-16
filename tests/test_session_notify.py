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
