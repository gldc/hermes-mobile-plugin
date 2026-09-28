"""Concurrency tests for DeviceStore (spec §9.1, review M7).

Every mutator is a load→modify→save of one JSON file. Several DeviceStore
instances share that file inside one process (the auth provider's and
plugin_api's), and the `hermes mobile` CLI writes it from another process.
HERMES_MOBILE_LOCKTEST_DIR relocates the store (e.g. onto Unraid shfs).
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path

import pytest

import hermes_mobile.device_store as ds
from hermes_mobile.device_store import DeviceStore

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def store_dir(tmp_path):
    base = os.environ.get("HERMES_MOBILE_LOCKTEST_DIR", "").strip()
    if not base:
        yield tmp_path / "mobile"
        return
    d = Path(base) / f"locktest-{uuid.uuid4().hex[:8]}"
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store_path(store_dir):
    return store_dir / "devices.json"


def _seed(path):
    s = DeviceStore(path=path)
    x_id, x_rt = s.create_device("x")
    y_id, y_rt = s.create_device("y")
    return x_id, x_rt, y_id, y_rt


def _race(monkeypatch, first, second, *, window=0.5):
    """Run first() on thread A and second() on thread B, forcing B's load to land
    inside A's load→save window whenever nothing serializes the two.

    A's first _save waits (up to *window*) until B has loaded. Unlocked, B loads
    the stale file and one update is lost. Locked, B cannot load until A has
    saved, so A just times out the wait and both updates survive.
    """
    a_saving = threading.Event()
    b_loaded = threading.Event()
    real_load, real_save = DeviceStore._load, DeviceStore._save

    def load(self):
        data = real_load(self)
        if threading.current_thread().name == "race-B":
            b_loaded.set()
        return data

    def save(self, data):
        if threading.current_thread().name == "race-A" and not a_saving.is_set():
            a_saving.set()
            b_loaded.wait(window)
        real_save(self, data)

    results, errors = {}, []

    def run(label, fn):
        try:
            results[label] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append((label, repr(exc)))

    with monkeypatch.context() as m:
        m.setattr(DeviceStore, "_load", load)
        m.setattr(DeviceStore, "_save", save)
        ta = threading.Thread(target=run, args=("A", first), name="race-A")
        tb = threading.Thread(target=run, args=("B", second), name="race-B")
        ta.start()
        assert a_saving.wait(5), "thread A never reached its save"
        tb.start()
        ta.join(15)
        tb.join(15)
        assert not ta.is_alive() and not tb.is_alive(), "race threads hung"
    assert errors == [], errors
    return results


def test_path_lock_is_shared_by_aliases_of_one_file(tmp_path):
    real = tmp_path / "mobile"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert ds._path_lock(real / "devices.json") is ds._path_lock(alias / "devices.json")
    assert ds._path_lock(real / "devices.json") is not ds._path_lock(
        real / "other.json"
    )


def test_race_rotate_vs_set_push_token_keeps_both(monkeypatch, store_path):
    x_id, x_rt, _, _ = _seed(store_path)
    a, b = DeviceStore(path=store_path), DeviceStore(path=store_path)
    r = _race(
        monkeypatch,
        lambda: a.rotate_refresh(x_rt),
        lambda: b.set_push_token(x_id, "ExponentPushToken[new]"),
    )
    fresh = DeviceStore(path=store_path)
    assert fresh.get_push_token(x_id) == "ExponentPushToken[new]"
    fresh.rotate_refresh(r["A"][1])  # UnknownRefreshTokenError if the rotation was lost


def test_race_two_devices_rotating_keeps_both(monkeypatch, store_path):
    _, x_rt, _, y_rt = _seed(store_path)
    a, b = DeviceStore(path=store_path), DeviceStore(path=store_path)
    r = _race(
        monkeypatch, lambda: a.rotate_refresh(x_rt), lambda: b.rotate_refresh(y_rt)
    )
    fresh = DeviceStore(path=store_path)
    fresh.rotate_refresh(r["A"][1])
    fresh.rotate_refresh(r["B"][1])


def test_race_logout_vs_rotate_keeps_both(monkeypatch, store_path):
    x_id, x_rt, _, y_rt = _seed(store_path)
    a, b = DeviceStore(path=store_path), DeviceStore(path=store_path)
    r = _race(
        monkeypatch, lambda: a.revoke_by_refresh(x_rt), lambda: b.rotate_refresh(y_rt)
    )
    assert r["A"] is True
    fresh = DeviceStore(path=store_path)
    assert fresh.get_device(x_id)["revoked"] is True
    fresh.rotate_refresh(r["B"][1])


def test_race_pair_vs_revoke_keeps_both(monkeypatch, store_path):
    _, _, y_id, _ = _seed(store_path)
    a, b = DeviceStore(path=store_path), DeviceStore(path=store_path)
    r = _race(monkeypatch, lambda: a.create_device("z"), lambda: b.revoke(y_id))
    z_id, _ = r["A"]
    fresh = DeviceStore(path=store_path)
    assert fresh.get_device(z_id) is not None
    assert fresh.get_device(y_id)["revoked"] is True
