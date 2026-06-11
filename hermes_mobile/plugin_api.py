"""Dashboard API routes for the mobile app — /api/plugins/mobile/...

Exposed as a module-level ``router`` (FastAPI APIRouter) and mounted by
the dashboard's plugin system via ``dashboard/manifest.json``'s ``api``
field (CONTRACTS.md §2.2) — the actual imported file is the thin shim
``dashboard/plugin_api.py``, which re-exports this router.

Every route requires a *mobile-device* session: the gated auth
middleware attaches the verified Session to ``request.state.session``;
we derive the device id from ``user_id == "mobile:<device_id>"`` and
reject anything else (other providers' sessions, or loopback mode where
no session exists) with 403. A browser logged in via GitHub OAuth has
no business draining a phone's mailbox.

Routes:
* ``POST /push-token {"token": ...}`` — register/refresh the calling
  device's Expo push token.
* ``GET /mailbox`` — return and drain the device's queued messages.
* ``GET /me`` — device self-info (no token hashes ever leave the store).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .auth_provider import PROVIDER_NAME
from .device_store import DeviceStore
from .mailbox import default_mailbox_dir, drain_messages, is_safe_device_id

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level singletons, injectable for tests via configure().
_store: Optional[DeviceStore] = None
_mailbox_dir: Optional[Path] = None


def configure(
    store: Optional[DeviceStore] = None,
    mailbox_dir: Optional[Path] = None,
) -> None:
    """Inject store/mailbox locations (tests); None resets to defaults."""
    global _store, _mailbox_dir
    _store = store
    _mailbox_dir = Path(mailbox_dir) if mailbox_dir is not None else None


def _get_store() -> DeviceStore:
    global _store
    if _store is None:
        _store = DeviceStore()
    return _store


def _get_mailbox_dir() -> Path:
    return _mailbox_dir if _mailbox_dir is not None else default_mailbox_dir()


def _require_device_id(request: Request) -> str:
    """Device id from the verified mobile-device session, else 403."""
    session = getattr(request.state, "session", None)
    if session is None:
        # Loopback/--insecure mode has no per-device identity — there is
        # no "calling device" to act on, so these routes are unusable.
        raise HTTPException(status_code=403, detail="mobile device session required")
    provider = getattr(session, "provider", "")
    user_id = getattr(session, "user_id", "") or ""
    if provider != PROVIDER_NAME or not user_id.startswith("mobile:"):
        raise HTTPException(status_code=403, detail="mobile device session required")
    device_id = user_id.split(":", 1)[1]
    if not is_safe_device_id(device_id):
        raise HTTPException(status_code=403, detail="mobile device session required")
    return device_id


class PushTokenBody(BaseModel):
    token: str


@router.post("/push-token")
def set_push_token(body: PushTokenBody, request: Request) -> Dict[str, Any]:
    """Register/refresh the calling device's Expo push token."""
    device_id = _require_device_id(request)
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="token must be non-empty")
    try:
        ok = _get_store().set_push_token(device_id, token)
    except Exception as exc:
        logger.warning("hermes-mobile: push-token store write failed: %s", exc)
        raise HTTPException(status_code=503, detail="device store unavailable")
    if not ok:
        raise HTTPException(status_code=404, detail="device unknown or revoked")
    return {"ok": True}


@router.get("/mailbox")
def get_mailbox(request: Request) -> Dict[str, List[Dict[str, Any]]]:
    """Return and drain the calling device's queued messages."""
    device_id = _require_device_id(request)
    try:
        messages = drain_messages(_get_mailbox_dir(), device_id)
    except Exception as exc:
        logger.warning("hermes-mobile: mailbox drain failed: %s", exc)
        raise HTTPException(status_code=503, detail="mailbox unavailable")
    return {"messages": messages}


@router.get("/me")
def me(request: Request) -> Dict[str, Any]:
    """Self-info for the calling device. Token hashes never leave the store."""
    device_id = _require_device_id(request)
    try:
        record = _get_store().get_device(device_id)
    except Exception as exc:
        logger.warning("hermes-mobile: device lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="device store unavailable")
    if record is None:
        raise HTTPException(status_code=404, detail="device unknown")
    return {
        "device_id": record.get("device_id", device_id),
        "name": record.get("name", ""),
        "created_at": record.get("created_at", 0),
        "last_refresh_at": record.get("last_refresh_at", 0),
        "revoked": bool(record.get("revoked", False)),
        "has_push_token": bool(record.get("push_token")),
    }
