"""Session-stop push notifications (docs/plans/session-stop-push-design.md).

Pings paired devices when a mobile-originated run stops / needs approval / asks a
clarify question, or a cron run finishes. Device attribution comes from a plugin-owned session-claim
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
from typing import Callable, List, Optional

from .device_store import DeviceStore
from .push import ExpoPush

logger = logging.getLogger(__name__)

SESSION_END_BODY = "Your session is ready — tap to check"
APPROVAL_BODY = "Hermes needs your approval"
CLARIFY_BODY = "Hermes has a question"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
#: One clarify push per (device, route session) per window. At 0.21.5 a clarify whose
#: attached clients are ALL pre-capabilities builds resolves empty at once and the model
#: may re-ask in a loop; with NO client attached it waits in open_requests for the
#: reconnect replay (tui_gateway/session_transports.py:38-47), which is when the push matters.
_CLARIFY_COOLDOWN_SECONDS = 30.0
_GATE_MEMORY_SECONDS = 60 * 60


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


class ClarifyPushGate:
    """Thread-safe cooldown: at most one clarify push per (device, route session)
    per ``cooldown_seconds``; entries older than an hour are forgotten."""

    def __init__(
        self,
        cooldown_seconds: float = _CLARIFY_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], float] = {}

    def allow(self, device_id: str, route_id: str) -> bool:
        now = self._clock()
        key = (device_id, route_id)
        with self._lock:
            self._last = {
                k: t for k, t in self._last.items() if now - t < _GATE_MEMORY_SECONDS
            }
            last = self._last.get(key)
            if last is not None and now - last < self._cooldown:
                return False
            self._last[key] = now
            return True


def _run_in_background(fn: Callable[[], None]) -> None:
    """Fire-and-forget on a daemon thread: the agent never waits on Expo."""
    threading.Thread(target=fn, name="hermes-mobile-clarify-push", daemon=True).start()


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
        clarify_gate: Optional[ClarifyPushGate] = None,
        background: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._store = store if store is not None else DeviceStore()
        self._push = push if push is not None else ExpoPush()
        self._registry = registry if registry is not None else get_registry()
        self._clarify_gate = (
            clarify_gate if clarify_gate is not None else ClarifyPushGate()
        )
        self._background = background if background is not None else _run_in_background

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
        self,
        session_key: Optional[str] = None,
        surface: Optional[str] = None,
        coalesced: bool = False,
        **_,
    ) -> None:
        if not _enabled() or surface != "gateway":
            return
        if coalesced:
            # A follower of an identical pending prompt (hermes coalesces parallel
            # duplicates; the leader already pushed). Both v2026.8.18 and v2026.9.24
            # fire this with coalesced=True.
            logger.debug(
                "hermes-mobile: session-notify approval coalesced "
                "(session_key=%s); skipping",
                session_key,
            )
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

    def on_pre_tool_call(
        self,
        tool_name: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        **_,
    ) -> None:
        """Observe-only ``pre_tool_call``: push when the agent asks a ``clarify``
        question in a claimed session.

        Always returns ``None`` (no directive) and never raises: from v2026.9.24 a
        ``pre_tool_call`` callback that raises, or runs past
        ``plugins.hook_callback_timeout`` (30 s), BLOCKS the tool. So the non-clarify
        path touches nothing, and the push itself runs off-thread.
        """
        try:
            if tool_name != "clarify" or not _enabled():
                return None
            hit = self._registry.resolve(session_id, task_id)
            if hit is None:
                logger.debug(
                    "hermes-mobile: session-notify clarify unclaimed "
                    "(session_id=%s task_id=%s); skipping",
                    session_id,
                    task_id,
                )
                return None
            device_id, route_id = hit
            if not self._clarify_gate.allow(device_id, route_id):
                logger.debug(
                    "hermes-mobile: session-notify clarify within cooldown for "
                    "device %s; skipping",
                    device_id,
                )
                return None
            logger.debug(
                "hermes-mobile: session-notify clarify claimed by device %s -> notifying",
                device_id,
            )
            self._background(
                lambda: self._safe_fan_out(
                    CLARIFY_BODY,
                    "clarify_request",
                    device_id=device_id,
                    session_id=route_id,
                )
            )
        except Exception:
            logger.debug("hermes-mobile: clarify push dispatch failed", exc_info=True)
        return None

    def _safe_fan_out(
        self,
        body: str,
        notif_type: str,
        *,
        device_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        try:
            self._fan_out(body, notif_type, device_id=device_id, session_id=session_id)
        except Exception:
            logger.debug("hermes-mobile: push fan-out failed", exc_info=True)

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
