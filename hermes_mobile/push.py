"""Expo push notifications — outbound-only, redacted by default.

The gateway never exposes anything inbound for push: it POSTs to Expo's
push API (``https://exp.host/--/api/v2/push/send``) and Expo/APNs do the
rest. Payloads are redacted by default ("New message from Hermes") so
message content never transits Expo/APNs unless the caller explicitly
opts in by passing a body.

Pure stdlib (``urllib.request``) so the module imports in every hermes
host process with zero extra dependencies; the HTTP transport is
injectable for tests. Network failures are logged and swallowed — push
is a best-effort signal, the mailbox is the source of truth, and a dead
push must never break message delivery.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
DEFAULT_TITLE = "Hermes"
#: Redacted default — content previews are an explicit caller opt-in.
DEFAULT_BODY = "New message from Hermes"

_TIMEOUT_SECONDS = 10.0

#: transport(url, data_bytes, headers) -> (status_code, response_text)
Transport = Callable[[str, bytes, dict], Tuple[int, str]]


def _urllib_transport(url: str, data: bytes, headers: dict) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # non-2xx still has a body
        return exc.code, exc.read().decode("utf-8", "replace")


class ExpoPush:
    """Tiny Expo push client. ``send`` never raises to the caller."""

    def __init__(self, transport: Optional[Transport] = None) -> None:
        self._transport = transport if transport is not None else _urllib_transport

    def send(
        self,
        token: str,
        title: str = DEFAULT_TITLE,
        body: Optional[str] = None,
    ) -> bool:
        """POST one notification to Expo. Returns True when Expo accepted it.

        *body* defaults to the redacted :data:`DEFAULT_BODY`; pass content
        only when the user opted in to previews. Failures (network, HTTP
        error, Expo per-ticket error) are logged at WARNING and reported
        as ``False`` — never raised.
        """
        if not token:
            return False
        payload = {
            "to": token,
            "title": title or DEFAULT_TITLE,
            "body": body if body is not None else DEFAULT_BODY,
        }
        try:
            status, response_text = self._transport(
                EXPO_PUSH_URL,
                json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json", "Accept": "application/json"},
            )
        except Exception as exc:
            logger.warning("hermes-mobile: Expo push failed (network): %s", exc)
            return False

        if status != 200:
            logger.warning(
                "hermes-mobile: Expo push rejected (HTTP %s): %.200s",
                status,
                response_text,
            )
            return False

        # Expo returns {"data": {"status": "ok"|"error", ...}} per message.
        try:
            data = json.loads(response_text).get("data")
            tickets = data if isinstance(data, list) else [data]
            for ticket in tickets:
                if isinstance(ticket, dict) and ticket.get("status") == "error":
                    logger.warning(
                        "hermes-mobile: Expo push ticket error: %s",
                        ticket.get("message", "unknown"),
                    )
                    return False
        except (ValueError, AttributeError):
            logger.warning("hermes-mobile: unparseable Expo push response")
            return False
        return True
