"""Device registry for the hermes-mobile plugin.

A small JSON-file store at ``~/.hermes/mobile/devices.json`` (path
injectable for tests) holding one record per paired mobile device.
Pure stdlib — no hermes imports — so it is unit-testable outside the
hermes process.

Token model (mirrors the dashboard auth middleware's cookie semantics):

* ``create_device(name)`` mints a device id and an initial 30-day
  refresh token (RT). No access token exists until the first rotation —
  the QR-delivered RT *is* the device credential, exchanged via
  ``rotate_refresh`` (which is what ``provider.refresh_session`` calls
  when the middleware sees a request with only the RT cookie).
* ``rotate_refresh(rt)`` rotates both tokens: a fresh ~15-minute access
  token (AT) and a fresh 30-day RT. The old RT hash is retired into
  ``prev_refresh_token_hashes``.
* **Reuse detection**: presenting a retired RT revokes the device
  (someone replayed a stolen token — kill the whole chain), matching
  hermes' rotating-RT conventions.

Only SHA-256 hashes of tokens are stored at rest; the file is written
atomically (a unique ``mkstemp`` sibling, fsynced, then ``os.replace``) with
owner-only permissions.

Writers are serialized by a process-wide lock keyed by the resolved path plus an
exclusive ``flock`` on the ``devices.json.lock`` sidecar (cross-process: the CLI).
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

#: Device ids are ``secrets.token_hex(DEVICE_ID_BYTES)``: 16 lowercase hex chars.
#: The adapter's ``mobile:<id>`` target parser is built from this, so it accepts
#: every id this store can mint (cron delivery depends on it).
DEVICE_ID_BYTES = 8

ACCESS_TTL_SECONDS = 15 * 60  # ~15-minute access tokens
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30-day rotating refresh tokens

# Refresh-token reuse policy is *distance-based*, not time-based. Each rotation
# supersedes the prior RT and retains its hash for reuse detection. Replaying
# the *immediately* prior RT means the client is exactly one rotation behind —
# it never durably received the last rotation's response (an aborted/lost
# response, or the app suspended before persisting the new token) — so we
# re-rotate forward instead of revoking, regardless of how much time has passed
# (a phone can resume from background hours or days later and must self-heal).
# Replaying an *older* rotated-out RT (two+ rotations back) is genuine reuse and
# revokes the device. Trade-off: forgiving the one-behind token slightly weakens
# detection of current-token theft — acceptable for a single-user client over a
# private VPN; the re-rotation is logged so a sustained ping-pong stays visible.

# How many rotated-out RT hashes to keep per device for reuse detection.
# Reuse of anything newer than this window revokes the device.
_MAX_PREV_HASHES = 50

#: How long a mutator waits for the store lock before raising DeviceStoreError
#: (the auth provider turns that into a transient ProviderError, never a re-pair).
LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DeviceStoreError(Exception):
    """Base class for device-store errors."""


class RefreshTokenError(DeviceStoreError):
    """Base class for rotate_refresh failures (→ RefreshExpiredError upstream)."""


class UnknownRefreshTokenError(RefreshTokenError):
    """RT not recognised (or its device is revoked)."""


class ExpiredRefreshTokenError(RefreshTokenError):
    """RT recognised but past its 30-day window."""


class ReusedRefreshTokenError(RefreshTokenError):
    """A rotated-out RT was replayed; the device has been revoked."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"refresh token reuse detected; device {device_id} revoked")
        self.device_id = device_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hashes_equal(a: str, b: str) -> bool:
    # Constant-time compare of hex digests (defence in depth; the inputs
    # are already one-way hashes).
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


# One lock per resolved store file, shared by every DeviceStore in the process.
_PATH_LOCKS: Dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    """The process-wide lock for *path* (symlinks and relative spellings resolve to one key)."""
    key = str(Path(path).resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


def _match_dir_owner(fd: int, directory: Path, name: Path) -> None:
    """When root writes the store (e.g. ``HERMES_DOCKER_EXEC_AS_ROOT=1``), hand the open
    file *fd* (``name``, for the log) to *directory*'s owner so the uid-10000 dashboard
    can still open it. No-op unless euid 0, or when *directory* is root-owned.

    Always by fd, never by path: the store dir is writable by the agent's uid, so a
    path could be swapped for a symlink to a root-owned file between open and chown.
    Only a directory or a single-link regular file is handed over, so a hardlink the
    agent planted to a root-owned file is refused (WARNING) rather than given away.
    Callers pass only what they just created; this check is defence in depth.
    """
    if os.geteuid() != 0:
        return
    try:
        fst = os.fstat(fd)
        if not (
            stat.S_ISDIR(fst.st_mode)
            or (stat.S_ISREG(fst.st_mode) and fst.st_nlink == 1)
        ):
            logger.warning(
                "hermes-mobile: refusing to chown %s to the store owner "
                "(not a single-link regular file or directory: nlink=%d)",
                name,
                fst.st_nlink,
            )
            return
        st = os.stat(directory)
        if st.st_uid != 0:
            os.fchown(fd, st.st_uid, st.st_gid)
    except OSError as exc:
        logger.warning(
            "hermes-mobile: could not chown %s to the store owner: %s", name, exc
        )


def default_devices_path() -> Path:
    """``<hermes home>/mobile/devices.json``.

    Uses hermes' canonical home resolution when running inside the hermes
    process; falls back to ``$HERMES_HOME`` / ``~/.hermes`` so this module
    stays importable without hermes on the path.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        home = Path(env) if env else Path.home() / ".hermes"
    return home / "mobile" / "devices.json"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class DeviceStore:
    """JSON-file-backed registry of paired mobile devices."""

    def __init__(
        self,
        path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._path = Path(path) if path is not None else default_devices_path()
        self._clock = clock
        self._lock_timeout = float(lock_timeout)

    @property
    def lock_path(self) -> Path:
        """Sidecar ``flock`` target (``devices.json.lock``) next to the store."""
        return self._path.with_name(self._path.name + ".lock")

    # ---- public API --------------------------------------------------------

    def create_device(self, name: str) -> Tuple[str, str]:
        """Mint a new device record. Returns ``(device_id, refresh_token)``.

        The refresh token is returned exactly once (for the pairing QR);
        only its hash is stored.
        """
        device_id = secrets.token_hex(DEVICE_ID_BYTES)
        refresh_token = secrets.token_urlsafe(32)
        now = self._now()
        with self._locked():
            data = self._load()
            data["devices"][device_id] = {
                "device_id": device_id,
                "name": str(name),
                "created_at": now,
                "revoked": False,
                "refresh_token_hash": _hash_token(refresh_token),
                "refresh_expires_at": now + REFRESH_TTL_SECONDS,
                "prev_refresh_token_hashes": [],
                "access_token_hash": "",
                "access_expires_at": 0,
                "last_refresh_at": 0,
                "push_token": "",
            }
            self._save(data)
        return device_id, refresh_token

    def rotate_refresh(self, refresh_token: str) -> Tuple[str, str, int]:
        """Exchange a live RT for ``(access_token, refresh_token, expires_at)``.

        Rotates both tokens. Replaying the immediately-prior RT re-rotates
        instead of revoking (the client is one rotation behind — a lost/retried
        response — and self-heals regardless of elapsed time; distance-based,
        not time-based). Raises:
            UnknownRefreshTokenError — RT unrecognised or device revoked
            ExpiredRefreshTokenError — RT past its 30-day window
            ReusedRefreshTokenError  — an older rotated-out RT (two+ rotations
                                       back) was replayed; the device is revoked
                                       as a side effect
            DeviceStoreError         — the store lock could not be taken in time
        """
        with self._locked():
            h = _hash_token(refresh_token or "")
            data = self._load()
            now = self._now()

            for dev in data["devices"].values():
                current_match = _hashes_equal(dev["refresh_token_hash"], h)
                prev_match = any(
                    _hashes_equal(prev, h)
                    for prev in dev.get("prev_refresh_token_hashes", [])
                )
                if not (current_match or prev_match):
                    continue
                if dev.get("revoked"):
                    raise UnknownRefreshTokenError("device is revoked")
                if prev_match:
                    prevs = dev.get("prev_refresh_token_hashes", [])
                    immediate_prior = bool(prevs) and _hashes_equal(prevs[0], h)
                    if not immediate_prior:
                        # A rotated-out token older than the immediate prior (two+
                        # rotations back): the chain is compromised → revoke.
                        dev["revoked"] = True
                        self._save(data)
                        raise ReusedRefreshTokenError(dev["device_id"])
                    # The immediately-prior RT: the client is exactly one rotation
                    # behind (it never durably received the last rotation's
                    # response). Re-rotate forward instead of revoking — independent
                    # of elapsed time. Logged so a sustained ping-pong stays visible.
                    logger.warning(
                        "device %s replayed the immediately-prior refresh token; "
                        "re-rotating (client was one rotation behind)",
                        dev["device_id"],
                    )
                if int(dev.get("refresh_expires_at", 0)) <= now:
                    raise ExpiredRefreshTokenError("refresh token expired")

                access_token = secrets.token_urlsafe(32)
                new_refresh = secrets.token_urlsafe(32)
                expires_at = now + ACCESS_TTL_SECONDS
                prev = [dev["refresh_token_hash"]] + list(
                    dev.get("prev_refresh_token_hashes", [])
                )
                dev["prev_refresh_token_hashes"] = prev[:_MAX_PREV_HASHES]
                dev["refresh_token_hash"] = _hash_token(new_refresh)
                dev["refresh_expires_at"] = now + REFRESH_TTL_SECONDS
                dev["access_token_hash"] = _hash_token(access_token)
                dev["access_expires_at"] = expires_at
                dev["last_refresh_at"] = now
                self._save(data)
                return access_token, new_refresh, expires_at

            raise UnknownRefreshTokenError("refresh token not recognised")

    def verify_access(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the device record for a live AT, else ``None``.

        Never raises for unrecognised tokens (providers stack — see
        DashboardAuthProvider.verify_session semantics).
        """
        if not access_token:
            return None
        h = _hash_token(access_token)
        data = self._load()
        now = self._now()
        for dev in data["devices"].values():
            if (
                not dev.get("revoked")
                and dev.get("access_token_hash")
                and _hashes_equal(dev["access_token_hash"], h)
                and int(dev.get("access_expires_at", 0)) > now
            ):
                return dict(dev)
        return None

    def revoke(self, device_id: str) -> None:
        """Revoke a device by id. No-op for unknown ids."""
        with self._locked():
            data = self._load()
            dev = data["devices"].get(device_id)
            if dev is None:
                return
            dev["revoked"] = True
            self._save(data)

    def revoke_by_refresh(self, refresh_token: str) -> bool:
        """Best-effort revoke by RT (current or rotated-out). True if found."""
        with self._locked():
            h = _hash_token(refresh_token or "")
            data = self._load()
            for dev in data["devices"].values():
                if _hashes_equal(dev["refresh_token_hash"], h) or any(
                    _hashes_equal(prev, h)
                    for prev in dev.get("prev_refresh_token_hashes", [])
                ):
                    dev["revoked"] = True
                    self._save(data)
                    return True
            return False

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Copy of one device record by id, or ``None`` if unknown."""
        data = self._load()
        dev = data["devices"].get(device_id)
        return dict(dev) if dev is not None else None

    def set_push_token(self, device_id: str, token: str) -> bool:
        """Store/refresh the device's Expo push token.

        Returns False for unknown or revoked devices. The push token is
        stored as-is (it is needed verbatim to call Expo's push API; it
        is not a credential against this gateway).
        """
        with self._locked():
            data = self._load()
            dev = data["devices"].get(device_id)
            if dev is None or dev.get("revoked"):
                return False
            dev["push_token"] = str(token or "")
            self._save(data)
            return True

    def get_push_token(self, device_id: str) -> Optional[str]:
        """The device's Expo push token, or ``None`` if unset/unknown/revoked."""
        data = self._load()
        dev = data["devices"].get(device_id)
        if dev is None or dev.get("revoked"):
            return None
        token = str(dev.get("push_token", "") or "")
        return token or None

    def list_devices(self) -> List[Dict[str, Any]]:
        """All device records (copies), token hashes included (hashes only)."""
        data = self._load()
        return [dict(dev) for dev in data["devices"].values()]

    # ---- internals ---------------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _load(self) -> Dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "devices": {}}
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
            raise DeviceStoreError(f"malformed device store at {self._path}")
        return data

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the store for one load→modify→save. Do not nest.

        Threads: the process-wide lock for the resolved path (0.21.5 refreshes in a
        threadpool, and the dashboard holds several DeviceStore instances).
        Processes: an exclusive ``flock`` on :attr:`lock_path` (``hermes mobile
        pair``/``revoke``). The kernel drops a flock when its holder dies, so a
        crash cannot leave a stale lock. Both waits share ONE deadline, so the total
        wait never exceeds ``lock_timeout`` (at 8.18 refresh runs on the event loop).
        """
        deadline = time.monotonic() + self._lock_timeout
        thread_lock = _path_lock(self._path)
        if not thread_lock.acquire(timeout=self._lock_timeout):
            raise DeviceStoreError(
                f"timed out after {self._lock_timeout:g}s waiting for {self._path}"
            )
        try:
            fd = self._open_lock_file()
            try:
                if fd is not None:
                    self._flock(fd, deadline)
                yield
            finally:
                if fd is not None:
                    # Unlock explicitly: a bare fork() child sharing this open file
                    # description would otherwise keep the flock alive after close.
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(fd)
        finally:
            thread_lock.release()

    def _open_lock_file(self) -> Optional[int]:
        """Open :attr:`lock_path`, or ``None`` (WARNING) to degrade to the thread lock.

        Only a lock file this call created (``O_EXCL``) is handed to the store owner:
        an existing one may be a root-owned file the agent renamed into place.
        """
        self._ensure_dir()
        exc: Optional[OSError] = None
        for _ in range(2):  # one retry: the file can vanish between the two opens
            try:
                # O_NOFOLLOW: a planted symlink (EEXIST, then ELOOP) degrades below.
                fd = os.open(
                    self.lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                try:
                    return os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
                except FileNotFoundError as missing:
                    exc = missing
                    continue
                except OSError as other:
                    exc = other
            except OSError as other:
                exc = other
            else:
                _match_dir_owner(fd, self._path.parent, self.lock_path)
                return fd
            break
        logger.warning(
            "hermes-mobile: cannot open %s (%s); cross-process locking disabled "
            "for this write",
            self.lock_path,
            exc,
        )
        return None

    def _flock(self, fd: int, deadline: float) -> None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeviceStoreError(
                        f"timed out after {self._lock_timeout:g}s waiting for "
                        f"{self.lock_path}"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
            except OSError as exc:
                logger.warning(
                    "hermes-mobile: flock unsupported on %s (%s); cross-process "
                    "locking disabled for this write",
                    self.lock_path,
                    exc,
                )
                return

    def _ensure_dir(self) -> None:
        directory = self._path.parent
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed and os.geteuid() == 0:
            # Root just created this directory (fresh install/reset): it is
            # root-owned, which would make _match_dir_owner's later checks on
            # *this* directory's owner (for the lock file and the store itself)
            # see uid 0 and silently no-op. Hand it to its own parent's owner now,
            # through an O_NOFOLLOW directory fd so a swapped-in symlink is refused.
            try:
                dir_fd = os.open(
                    directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except OSError as exc:
                logger.warning(
                    "hermes-mobile: could not chown %s to the store owner: %s",
                    directory,
                    exc,
                )
            else:
                try:
                    _match_dir_owner(dir_fd, directory.parent, directory)
                finally:
                    os.close(dir_fd)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    def _save(self, data: Dict[str, Any]) -> None:
        """Atomically replace the store: unique ``mkstemp`` sibling (0600), fsync, rename."""
        self._ensure_dir()
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                _match_dir_owner(fh.fileno(), self._path.parent, self._path)
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
