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
atomically with owner-only permissions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ACCESS_TTL_SECONDS = 15 * 60  # ~15-minute access tokens
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30-day rotating refresh tokens

# How many rotated-out RT hashes to keep per device for reuse detection.
# Reuse of anything newer than this window revokes the device.
_MAX_PREV_HASHES = 50


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
    ) -> None:
        self._path = Path(path) if path is not None else default_devices_path()
        self._clock = clock

    # ---- public API --------------------------------------------------------

    def create_device(self, name: str) -> Tuple[str, str]:
        """Mint a new device record. Returns ``(device_id, refresh_token)``.

        The refresh token is returned exactly once (for the pairing QR);
        only its hash is stored.
        """
        device_id = secrets.token_hex(8)
        refresh_token = secrets.token_urlsafe(32)
        now = self._now()
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
        }
        self._save(data)
        return device_id, refresh_token

    def rotate_refresh(self, refresh_token: str) -> Tuple[str, str, int]:
        """Exchange a live RT for ``(access_token, refresh_token, expires_at)``.

        Rotates both tokens. Raises:
            UnknownRefreshTokenError — RT unrecognised or device revoked
            ExpiredRefreshTokenError — RT past its 30-day window
            ReusedRefreshTokenError  — RT was already rotated out; the
                                       device is revoked as a side effect
        """
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
                # Reuse of a rotated-out token: the chain is compromised.
                dev["revoked"] = True
                self._save(data)
                raise ReusedRefreshTokenError(dev["device_id"])
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
        """Revoke a device by id. No-op for unknown ids; never raises."""
        data = self._load()
        dev = data["devices"].get(device_id)
        if dev is None:
            return
        dev["revoked"] = True
        self._save(data)

    def revoke_by_refresh(self, refresh_token: str) -> bool:
        """Best-effort revoke by RT (current or rotated-out). True if found."""
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

    def _save(self, data: Dict[str, Any]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self._path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
