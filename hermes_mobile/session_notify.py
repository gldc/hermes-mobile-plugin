"""Session-stop push notifications (docs/plans/session-stop-push-design.md).

Pings paired devices when a mobile-originated run stops / needs approval, or a
cron run finishes. Device attribution comes from a plugin-owned session-claim
route (the app calls it after session.create/resume); the hooks resolve the
resulting in-process registry. No gateway import at module top, so this loads in
every host process; gateway-only helpers are imported lazily. Best-effort:
failures are logged and never affect the agent run.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import List, Optional

from .device_store import DeviceStore
from .push import ExpoPush

logger = logging.getLogger(__name__)

SESSION_END_BODY = "Your session is ready — tap to check"
APPROVAL_BODY = "Hermes needs your approval"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_TTL_SECONDS = 24 * 60 * 60


class SessionClaimRegistry:
    """In-process, thread-safe TTL map: session_id / session_key -> device_id."""

    def __init__(
        self, ttl_seconds: int = _DEFAULT_TTL_SECONDS, clock=time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, tuple[str, str, float]] = {}

    def claim(
        self, device_id: str, *ids: Optional[str], route_id: Optional[str] = None
    ) -> None:
        """Bind every id in *ids* to *device_id* and the canonical *route_id*.

        *route_id* is the stored/route session id the app navigates on (its
        `session_key`); when omitted it falls back to the first non-empty id.
        Retaining it lets `on_session_end` (which sees only the live id) emit
        the stored id deterministically.
        """
        if not device_id:
            return
        route = (route_id or next((str(i) for i in ids if i), "")) or ""
        expires = self._clock() + self._ttl
        with self._lock:
            for i in ids:
                if i:
                    self._by_id[str(i)] = (device_id, route, expires)

    def resolve(self, *ids: Optional[str]) -> Optional[tuple[str, str]]:
        """First non-expired match → (device_id, route_id), else None."""
        now = self._clock()
        with self._lock:
            for i in ids:
                if not i:
                    continue
                hit = self._by_id.get(str(i))
                if hit is not None and hit[2] > now:
                    return (hit[0], hit[1])
            return None


_registry = SessionClaimRegistry()


def get_registry() -> SessionClaimRegistry:
    """The process-wide registry shared by the session-claim route and hooks."""
    return _registry


def _enabled() -> bool:
    return (
        os.getenv("MOBILE_NOTIFY_ON_SESSION_END", "1").strip().lower()
        not in _DISABLED_VALUES
    )


def _is_cron_run() -> bool:
    return os.getenv("HERMES_CRON_SESSION", "").strip() == "1"


def _already_delivered_to_mobile() -> bool:
    """HERMES_CRON_AUTO_DELIVER_PLATFORM is a ContextVar, not an env var — read it
    via the gateway's session-context accessor (gateway is present in the gateway
    process where cron's on_session_end fires). Lazy import keeps this module
    gateway-free at import time."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return False
    return (
        str(get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "") or "")
        .strip()
        .lower()
        == "mobile"
    )


class SessionNotifier:
    def __init__(
        self,
        store: Optional[DeviceStore] = None,
        push: Optional[ExpoPush] = None,
        registry: Optional[SessionClaimRegistry] = None,
    ) -> None:
        self._store = store if store is not None else DeviceStore()
        self._push = push if push is not None else ExpoPush()
        self._registry = registry if registry is not None else get_registry()

    def on_session_end(
        self,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        interrupted: bool = False,
        **_,
    ) -> None:
        if not _enabled() or interrupted:
            return
        if _is_cron_run():
            if _already_delivered_to_mobile():
                logger.debug(
                    "hermes-mobile: session-notify cron end already delivered to "
                    "mobile; skipping"
                )
                return
            logger.debug("hermes-mobile: session-notify cron end -> notifying devices")
            self._fan_out(SESSION_END_BODY, "session_end")  # broadcast, no id
            return

        hit = self._registry.resolve(session_id, task_id)
        if hit is None:
            logger.debug(
                "hermes-mobile: session-notify session end unclaimed "
                "(session_id=%s task_id=%s); skipping",
                session_id,
                task_id,
            )
            return
        device_id, route_id = hit
        logger.debug(
            "hermes-mobile: session-notify session end claimed by device %s "
            "-> notifying",
            device_id,
        )
        self._fan_out(
            SESSION_END_BODY, "session_end", device_id=device_id, session_id=route_id
        )

    def on_pre_approval_request(
        self, session_key: Optional[str] = None, surface: Optional[str] = None, **_
    ) -> None:
        if not _enabled() or surface != "gateway":
            return
        hit = self._registry.resolve(session_key)
        if hit is None:
            logger.debug(
                "hermes-mobile: session-notify approval unclaimed "
                "(session_key=%s); skipping",
                session_key,
            )
            return
        device_id, route_id = hit
        logger.debug(
            "hermes-mobile: session-notify approval claimed by device %s -> notifying",
            device_id,
        )
        self._fan_out(
            APPROVAL_BODY, "approval_request", device_id=device_id, session_id=route_id
        )

    def _tokened_devices(self) -> List[dict]:
        try:
            return [
                d
                for d in self._store.list_devices()
                if not d.get("revoked") and d.get("push_token")
            ]
        except Exception:
            logger.debug("hermes-mobile: list_devices failed", exc_info=True)
            return []

    def _fan_out(
        self,
        body: str,
        notif_type: str,
        *,
        device_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Send a redacted push. With *device_id* (a claimed session) target that
        one device and include the route *session_id* in ``data``; otherwise
        (cron) broadcast to every tokened device with id-less data."""
        data = {"type": notif_type}
        if session_id:
            data["session_id"] = session_id
        if device_id is not None:
            token = self._store.get_push_token(device_id)
            if token:
                try:
                    self._push.send(token, body=body, data=data)
                except Exception:
                    logger.debug("hermes-mobile: push send failed", exc_info=True)
            return
        for d in self._tokened_devices():
            try:
                self._push.send(d["push_token"], body=body, data=data)
            except Exception:
                logger.debug("hermes-mobile: push send failed", exc_info=True)
