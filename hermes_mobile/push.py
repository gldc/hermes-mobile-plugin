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
import re
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

_REDACTED = "[redacted]"
#: Expo token shapes (``ExponentPushToken[...]``, ``ExpoPushToken[...]``), matched
#: loosely: any case, optional whitespace before the bracket, a literal or
#: JSON-escaped (``\u005b``/``\u005d``) bracket, and no closing bracket needed (a
#: body cut off mid-token). The id stops at ``]``, whitespace, a quote or a
#: backslash (the start of an escaped closing bracket).
_EXPO_TOKEN_RE = re.compile(
    r"(?i)(expo(?:nent)?pushtoken)\s*(?:\[|\\u005b)[^\]\s\"\\]*(?:\]|\\u005d)?"
)
#: A logged fragment this long that starts the device's own (non-Expo) token is
#: treated as that token, truncated.
_OWN_TOKEN_PREFIX_LEN = 8


def _mask_own_token(text: str, token: str) -> str:
    """Mask the device's own *token*, including a copy cut short by truncation.

    Every occurrence of the token's first ``_OWN_TOKEN_PREFIX_LEN`` characters is
    masked together with however much of the rest of the token follows it.
    Shorter tokens are masked only where they appear whole.
    """
    if len(token) < _OWN_TOKEN_PREFIX_LEN:
        return text.replace(token, _REDACTED)
    head = token[:_OWN_TOKEN_PREFIX_LEN]
    parts = []
    pos = 0
    while (hit := text.find(head, pos)) != -1:
        end = hit + len(head)
        matched = len(head)
        while end < len(text) and matched < len(token) and text[end] == token[matched]:
            end += 1
            matched += 1
        parts += [text[pos:hit], _REDACTED]
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _redact(text: object, token: str) -> str:
    """*text* with push tokens replaced before it reaches a log.

    Expo echoes the token in error tickets (``DeviceNotRegistered``) and error
    bodies. The token is a push credential for the device and has no place in
    gateway or CLI logs. Masked: the device's own token (whole, or a truncated
    copy when it is not Expo-shaped) and any Expo-shaped token, including
    another device's.
    """
    out = str(text)
    if token:
        out = out.replace(token, _REDACTED)
    out = _EXPO_TOKEN_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    if token and not _EXPO_TOKEN_RE.fullmatch(token):
        # Expo-shaped tokens are fully covered by the pattern above, including
        # a cut-off copy; scanning for their "ExponentP" prefix would instead
        # mangle the markers it just wrote.
        out = _mask_own_token(out, token)
    return out


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
        data: Optional[dict] = None,
    ) -> bool:
        """POST one notification to Expo. Returns True when Expo accepted it.

        *body* defaults to the redacted :data:`DEFAULT_BODY`; pass content
        only when the user opted in to previews. *data* (optional) is the
        Expo ``data`` field carrying **routing only** — never content; it
        rides redacted (e.g. ``{"type": "session_end"}``) so the app can
        decide how to handle a notification without leaking anything.
        Failures (network, HTTP error, Expo per-ticket error) are logged at
        WARNING and reported as ``False`` — never raised.
        """
        if not token:
            return False
        payload = {
            "to": token,
            "title": title or DEFAULT_TITLE,
            "body": body if body is not None else DEFAULT_BODY,
        }
        if data is not None:
            payload["data"] = data
        try:
            status, response_text = self._transport(
                EXPO_PUSH_URL,
                json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json", "Accept": "application/json"},
            )
        except Exception as exc:
            logger.warning(
                "hermes-mobile: Expo push failed (network): %s", _redact(exc, token)
            )
            return False

        if status != 200:
            logger.warning(
                "hermes-mobile: Expo push rejected (HTTP %s): %.200s",
                status,
                _redact(response_text, token),  # redact first, then truncate
            )
            return False

        # Expo returns {"data": {"status": "ok"|"error", ...}} per message.
        try:
            data = json.loads(response_text).get("data")
            tickets = data if isinstance(data, list) else [data]
            for ticket in tickets:
                if isinstance(ticket, dict) and ticket.get("status") == "error":
                    details = ticket.get("details")
                    code = details.get("error") if isinstance(details, dict) else None
                    logger.warning(
                        "hermes-mobile: Expo push ticket error%s: %s",
                        f" ({_redact(code, token)})" if code else "",
                        _redact(ticket.get("message", "unknown"), token),
                    )
                    return False
        except (ValueError, AttributeError):
            logger.warning("hermes-mobile: unparseable Expo push response")
            return False
        return True
