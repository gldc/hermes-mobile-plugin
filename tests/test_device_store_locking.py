"""Concurrency tests for DeviceStore (spec §9.1, review M7).

Every mutator is a load→modify→save of one JSON file. Several DeviceStore
instances share that file inside one process (the auth provider's and
plugin_api's), and the `hermes mobile` CLI writes it from another process.
HERMES_MOBILE_LOCKTEST_DIR relocates the store (e.g. onto Unraid shfs).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import hermes_mobile.device_store as ds
from hermes_mobile.device_store import DeviceStore, DeviceStoreError

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


# ---- cross-process ------------------------------------------------------

_WRITER = r"""
import sys, time
from pathlib import Path
from hermes_mobile.device_store import DeviceStore
path, gate, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real_save = DeviceStore._save
def slow_save(self, data):
    time.sleep(0.3)
    real_save(self, data)
DeviceStore._save = slow_save
while not gate.exists():
    time.sleep(0.005)
DeviceStore(path=path).create_device(name)
"""

_HOLDER = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(600)
"""


def _child_env():
    extra = os.environ.get("PYTHONPATH", "")
    return dict(
        os.environ, PYTHONPATH=os.pathsep.join(p for p in (str(REPO_ROOT), extra) if p)
    )


def test_lock_path_is_a_sidecar_next_to_the_store(store_path):
    s = DeviceStore(path=store_path)
    assert s.lock_path == store_path.with_name("devices.json.lock")
    s.create_device("a")
    assert s.lock_path.exists()
    assert s.lock_path.stat().st_mode & 0o777 == 0o600


def test_cross_process_writers_do_not_lose_updates(store_dir, store_path):
    # Three CLI-like processes pair at once; each widens its load→save window to
    # 0.3 s. Unlocked, every one loads the empty store and the last save wins.
    store_dir.mkdir(parents=True, exist_ok=True)
    gate = store_dir / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(store_path), str(gate), f"proc{i}"],
            env=_child_env(),
        )
        for i in range(3)
    ]
    try:
        time.sleep(1.0)
        gate.touch()
        assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    names = sorted(d["name"] for d in DeviceStore(path=store_path).list_devices())
    assert names == ["proc0", "proc1", "proc2"]


def test_lock_held_by_a_killed_process_is_released(store_dir, store_path):
    store_dir.mkdir(parents=True, exist_ok=True)
    store = DeviceStore(path=store_path, lock_timeout=5.0)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(store.lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        impatient = DeviceStore(path=store_path, lock_timeout=0.2)
        with pytest.raises(DeviceStoreError, match="timed out"):
            impatient.create_device("while-held")
        holder.kill()  # SIGKILL: no cleanup code runs in the holder
        holder.wait(timeout=10)
        device_id, _ = store.create_device("after-crash")
        assert store.get_device(device_id)["name"] == "after-crash"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        holder.stdout.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_unopenable_lock_file_degrades_to_thread_lock(store_path, caplog):
    s = DeviceStore(path=store_path)
    s.create_device("first")
    os.chmod(s.lock_path, 0)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_mobile.device_store"):
            device_id, _ = s.create_device("second")
        assert s.get_device(device_id)["name"] == "second"
        assert "cross-process locking disabled" in caplog.text
    finally:
        os.chmod(s.lock_path, 0o600)


def test_flock_unsupported_degrades_to_thread_lock(store_path, monkeypatch, caplog):
    s = DeviceStore(path=store_path)

    def no_flock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(ds.fcntl, "flock", no_flock)
    with caplog.at_level(logging.WARNING, logger="hermes_mobile.device_store"):
        device_id, _ = s.create_device("a")
    assert s.get_device(device_id)["name"] == "a"
    assert "cross-process locking disabled" in caplog.text


def test_root_writes_hand_files_to_the_store_dir_owner(store_path, monkeypatch):
    # `docker exec` is root on dc1-1; a root-owned 0600 devices.json or lock file
    # would lock the uid-10000 dashboard out of every device.
    store_path.parent.mkdir(parents=True, exist_ok=True)
    s = DeviceStore(path=store_path)
    if os.geteuid() == 0:
        os.chown(store_path.parent, 10000, 10000)
        s.create_device("a")
        for p in (store_path, s.lock_path):
            st = p.stat()
            assert (st.st_uid, st.st_gid) == (10000, 10000), p
        return
    calls = []
    monkeypatch.setattr(ds.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        ds.os, "chown", lambda p, uid, gid: calls.append((Path(p).name, uid, gid))
    )
    s.create_device("a")
    st = store_path.parent.stat()
    names = {c[0] for c in calls}
    assert "devices.json.lock" in names
    assert any(n.startswith(".devices.json.") and n.endswith(".tmp") for n in names)
    assert all((uid, gid) == (st.st_uid, st.st_gid) for _, uid, gid in calls)
