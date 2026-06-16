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
        self._by_id: dict[str, tuple[str, float]] = {}

    def claim(self, device_id: str, *ids: Optional[str]) -> None:
        if not device_id:
            return
        expires = self._clock() + self._ttl
        with self._lock:
            for i in ids:
                if i:
                    self._by_id[str(i)] = (device_id, expires)

    def resolve(self, *ids: Optional[str]) -> Optional[str]:
        now = self._clock()
        with self._lock:
            for i in ids:
                if not i:
                    continue
                hit = self._by_id.get(str(i))
                if hit is not None and hit[1] > now:
                    return hit[0]
            return None


_registry = SessionClaimRegistry()


def get_registry() -> SessionClaimRegistry:
    """The process-wide registry shared by the session-claim route and hooks."""
    return _registry
