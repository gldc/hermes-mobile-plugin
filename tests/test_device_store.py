"""Tests for hermes_mobile.device_store — pure-stdlib device registry.

Run from the repo root:
    PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -x -q
(the store itself has no hermes imports; PYTHONPATH is needed for the
auth-provider tests in this suite's sibling module).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from hermes_mobile.device_store import (
    ACCESS_TTL_SECONDS,
    GRACE_REUSE_SECONDS,
    REFRESH_TTL_SECONDS,
    DeviceStore,
    ExpiredRefreshTokenError,
    RefreshTokenError,
    ReusedRefreshTokenError,
    UnknownRefreshTokenError,
)


class FakeClock:
    def __init__(self, t: int = 1_750_000_000) -> None:
        self.t = t

    def __call__(self) -> float:
        return float(self.t)

    def advance(self, seconds: int) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "mobile" / "devices.json"


@pytest.fixture
def store(store_path, clock) -> DeviceStore:
    return DeviceStore(path=store_path, clock=clock)


# ---------------------------------------------------------------------------
# create_device / list_devices
# ---------------------------------------------------------------------------


def test_create_device_returns_id_and_refresh_token(store):
    device_id, refresh_token = store.create_device("Gianluca's iPhone")
    assert device_id
    assert refresh_token
    # token_urlsafe(32) yields ~43 chars; require meaningful entropy.
    assert len(refresh_token) >= 40


def test_create_device_ids_and_tokens_are_unique(store):
    pairs = [store.create_device(f"dev{i}") for i in range(5)]
    ids = {p[0] for p in pairs}
    tokens = {p[1] for p in pairs}
    assert len(ids) == 5
    assert len(tokens) == 5


def test_tokens_stored_as_sha256_hashes_only(store, store_path):
    device_id, refresh_token = store.create_device("phone")
    raw = store_path.read_text()
    assert refresh_token not in raw
    expected_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    assert expected_hash in raw
    # sanity: the file is JSON with our device in it
    data = json.loads(raw)
    assert device_id in data["devices"]


def test_access_tokens_stored_hashed_only(store, store_path):
    _, rt = store.create_device("phone")
    access_token, new_rt, _ = store.rotate_refresh(rt)
    raw = store_path.read_text()
    assert access_token not in raw
    assert new_rt not in raw
    assert hashlib.sha256(access_token.encode()).hexdigest() in raw


def test_list_devices(store, clock):
    id1, _ = store.create_device("phone")
    id2, _ = store.create_device("tablet")
    devices = store.list_devices()
    by_id = {d["device_id"]: d for d in devices}
    assert set(by_id) == {id1, id2}
    assert by_id[id1]["name"] == "phone"
    assert by_id[id2]["name"] == "tablet"
    assert by_id[id1]["created_at"] == int(clock.t)
    assert by_id[id1]["revoked"] is False


def test_devices_file_is_owner_only(store, store_path):
    store.create_device("phone")
    mode = store_path.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"devices.json is group/world accessible: {oct(mode)}"


# ---------------------------------------------------------------------------
# rotate_refresh
# ---------------------------------------------------------------------------


def test_rotate_refresh_returns_new_tokens_and_expiry(store, clock):
    _, rt = store.create_device("phone")
    access_token, new_rt, expires_at = store.rotate_refresh(rt)
    assert access_token and new_rt
    assert new_rt != rt
    assert expires_at == int(clock.t) + ACCESS_TTL_SECONDS


def test_rotate_refresh_unknown_token_raises(store):
    store.create_device("phone")
    with pytest.raises(UnknownRefreshTokenError):
        store.rotate_refresh("not-a-real-token")


def test_rotate_refresh_expired_rt_raises(store, clock):
    _, rt = store.create_device("phone")
    clock.advance(REFRESH_TTL_SECONDS + 1)
    with pytest.raises(ExpiredRefreshTokenError):
        store.rotate_refresh(rt)


def test_rotation_extends_refresh_window(store, clock):
    _, rt = store.create_device("phone")
    clock.advance(REFRESH_TTL_SECONDS - 60)
    _, rt2, _ = store.rotate_refresh(rt)
    # The new RT lives a fresh 30 days from rotation time.
    clock.advance(REFRESH_TTL_SECONDS - 60)
    at, _, _ = store.rotate_refresh(rt2)
    assert store.verify_access(at) is not None


# ---------------------------------------------------------------------------
# reuse detection
# ---------------------------------------------------------------------------


def test_refresh_token_reuse_revokes_device(store, clock):
    device_id, rt1 = store.create_device("phone")
    at, rt2, _ = store.rotate_refresh(rt1)
    # Replaying the rotated-out RT after the grace window is reuse → revoked.
    clock.advance(GRACE_REUSE_SECONDS + 1)
    with pytest.raises(ReusedRefreshTokenError):
        store.rotate_refresh(rt1)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True
    # The current RT and AT are dead too.
    with pytest.raises(RefreshTokenError):
        store.rotate_refresh(rt2)
    assert store.verify_access(at) is None


def test_reuse_error_carries_device_id(store, clock):
    device_id, rt1 = store.create_device("phone")
    store.rotate_refresh(rt1)
    clock.advance(GRACE_REUSE_SECONDS + 1)  # past the rotation-race grace window
    with pytest.raises(ReusedRefreshTokenError) as excinfo:
        store.rotate_refresh(rt1)
    assert excinfo.value.device_id == device_id


# ---------------------------------------------------------------------------
# reuse detection — rotation-race grace window
# ---------------------------------------------------------------------------


def test_immediate_prev_replay_within_grace_rerotates(store):
    # A duplicate/concurrent refresh that replays the immediately-prior RT
    # within the grace window is benign (e.g. an out-of-process retry or a
    # network-layer resend): it re-rotates instead of revoking the device.
    _, rt1 = store.create_device("phone")
    _, rt2, _ = store.rotate_refresh(rt1)
    at3, rt3, _ = store.rotate_refresh(rt1)  # replay rt1 immediately (within grace)
    assert rt3 and rt3 != rt2
    assert store.verify_access(at3) is not None
    (dev,) = store.list_devices()
    assert dev["revoked"] is False


def test_prev_replay_after_grace_revokes(store, clock):
    # Outside the window, replaying the immediately-prior RT is still treated
    # as theft and revokes the device.
    _, rt1 = store.create_device("phone")
    store.rotate_refresh(rt1)
    clock.advance(GRACE_REUSE_SECONDS + 1)
    with pytest.raises(ReusedRefreshTokenError):
        store.rotate_refresh(rt1)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True


def test_older_prev_replay_revokes_even_within_grace(store):
    # Grace forgives only the *immediately* prior RT. Replaying an older
    # rotated-out RT (two+ rotations back) is still a compromise, even inside
    # the window.
    _, rt1 = store.create_device("phone")
    _, rt2, _ = store.rotate_refresh(rt1)
    store.rotate_refresh(rt2)  # rt1 is now prev[1], not the immediate prior
    with pytest.raises(ReusedRefreshTokenError):
        store.rotate_refresh(rt1)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True


# ---------------------------------------------------------------------------
# verify_access
# ---------------------------------------------------------------------------


def test_verify_access_returns_device_record(store):
    device_id, rt = store.create_device("phone")
    at, _, expires_at = store.rotate_refresh(rt)
    record = store.verify_access(at)
    assert record is not None
    assert record["device_id"] == device_id
    assert record["name"] == "phone"
    assert record["access_expires_at"] == expires_at


def test_verify_access_unknown_token_returns_none(store):
    store.create_device("phone")
    assert store.verify_access("garbage") is None
    assert store.verify_access("") is None


def test_verify_access_expired_token_returns_none(store, clock):
    _, rt = store.create_device("phone")
    at, _, _ = store.rotate_refresh(rt)
    clock.advance(ACCESS_TTL_SECONDS + 1)
    assert store.verify_access(at) is None


def test_verify_access_before_first_rotation_returns_none(store):
    # create_device mints no access token; nothing should verify.
    _, rt = store.create_device("phone")
    assert store.verify_access(rt) is None


def test_rotation_invalidates_previous_access_token(store):
    _, rt = store.create_device("phone")
    at1, rt2, _ = store.rotate_refresh(rt)
    at2, _, _ = store.rotate_refresh(rt2)
    assert store.verify_access(at1) is None
    assert store.verify_access(at2) is not None


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_kills_access_and_refresh(store):
    device_id, rt = store.create_device("phone")
    at, rt2, _ = store.rotate_refresh(rt)
    store.revoke(device_id)
    assert store.verify_access(at) is None
    with pytest.raises(RefreshTokenError):
        store.rotate_refresh(rt2)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True


def test_revoke_unknown_device_is_noop(store):
    store.revoke("no-such-device")  # must not raise
    assert store.list_devices() == []


def test_revoke_by_refresh(store):
    device_id, rt = store.create_device("phone")
    assert store.revoke_by_refresh(rt) is True
    (dev,) = store.list_devices()
    assert dev["revoked"] is True
    assert store.revoke_by_refresh("garbage") is False


def test_revoke_by_refresh_matches_rotated_out_token(store):
    _, rt1 = store.create_device("phone")
    store.rotate_refresh(rt1)
    # Best-effort logout with a stale RT should still find the device.
    assert store.revoke_by_refresh(rt1) is True


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_state_persists_across_store_instances(store_path, clock):
    store1 = DeviceStore(path=store_path, clock=clock)
    device_id, rt = store1.create_device("phone")
    at, rt2, _ = store1.rotate_refresh(rt)

    store2 = DeviceStore(path=store_path, clock=clock)
    record = store2.verify_access(at)
    assert record is not None and record["device_id"] == device_id
    # Reuse detection survives reload too (replay past the grace window).
    clock.advance(GRACE_REUSE_SECONDS + 1)
    with pytest.raises(ReusedRefreshTokenError):
        store2.rotate_refresh(rt)


def test_missing_file_means_empty_store(store_path, clock):
    store = DeviceStore(path=store_path, clock=clock)
    assert store.list_devices() == []
    assert store.verify_access("anything") is None
