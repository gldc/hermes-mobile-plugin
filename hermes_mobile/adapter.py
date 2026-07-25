"""MobileAdapter — the 'mobile' gateway platform (mailbox + redacted push).

Registered via ``ctx.register_platform`` (CONTRACTS.md §1.3); makes a
paired phone a first-class ``send_message`` / cron-delivery target:
``chat_id`` is the device id. ``send()`` appends the message to the
device's mailbox file (``~/.hermes/mobile/mailbox/<device_id>.jsonl``)
and, when the device has registered an Expo push token, fires a
redacted push ("New message from Hermes") — content never transits
Expo/APNs; the app fetches it over the VPN by draining the mailbox.

This module imports gateway code, so it is only imported lazily from
the platform adapter_factory (the auth provider and CLI must keep
working in processes where the gateway package is absent).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .device_store import DeviceStore
from .mailbox import append_message, default_mailbox_dir, is_safe_device_id
from .push import ExpoPush

logger = logging.getLogger(__name__)

PLATFORM_NAME = "mobile"


class MobileAdapter(BasePlatformAdapter):
    """Outbound-only adapter: mailbox append + best-effort redacted push."""

    # Mailbox content is rendered by our own app — markdown passes through.
    supports_code_blocks = True

    def __init__(
        self,
        config,
        *,
        store: Optional[DeviceStore] = None,
        push: Optional[ExpoPush] = None,
        mailbox_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._store = store if store is not None else DeviceStore()
        self._push = push if push is not None else ExpoPush()
        self._mailbox_dir = (
            Path(mailbox_dir) if mailbox_dir is not None else default_mailbox_dir()
        )

    # ---- required abstract surface -----------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Nothing to connect: delivery is filesystem + outbound HTTPS.
        # ``is_reconnect`` only matters to adapters holding a server-side
        # update queue (Telegram's Bot API); the mailbox is durable on disk,
        # so nothing to preserve or drop either way. Accepting the kwarg is
        # mandatory: the gateway always passes it.
        return True

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not is_safe_device_id(chat_id):
            return SendResult(
                success=False,
                error=f"invalid mobile device id: {chat_id!r}",
            )
        try:
            record = append_message(
                self._mailbox_dir,
                chat_id,
                content,
                reply_to=reply_to,
                metadata=metadata,
            )
        except OSError as exc:
            logger.warning(
                "hermes-mobile: mailbox write failed for %s: %s", chat_id, exc
            )
            return SendResult(success=False, error=str(exc), retryable=True)

        self._maybe_push(chat_id)
        return SendResult(success=True, message_id=record["message_id"])

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        name = chat_id
        try:
            device = self._store.get_device(chat_id)
            if device is not None and device.get("name"):
                name = str(device["name"])
        except Exception:
            logger.debug(
                "hermes-mobile: get_chat_info store lookup failed", exc_info=True
            )
        return {"name": name, "type": "dm"}

    # ---- internals -----------------------------------------------------------

    def _maybe_push(self, device_id: str) -> None:
        """Fire a redacted Expo push if the device registered a token.

        Push is best-effort: any failure is logged inside ExpoPush /
        swallowed here and never affects the SendResult — the mailbox
        write already succeeded.
        """
        try:
            token = self._store.get_push_token(device_id)
        except Exception:
            logger.debug("hermes-mobile: push-token lookup failed", exc_info=True)
            return
        if not token:
            return
        self._push.send(token)  # redacted defaults; never raises


def check_requirements() -> bool:
    """register_platform check_fn — stdlib-only adapter, always available."""
    return True


def register_platform(ctx, store: DeviceStore) -> None:
    """Register the 'mobile' platform on the plugin context."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Mobile",
        adapter_factory=lambda cfg: MobileAdapter(cfg, store=store),
        check_fn=check_requirements,
        install_hint="No extra packages needed (stdlib only)",
        emoji="📱",
        # Lets cron / scheduled jobs deliver to the phone via ``deliver=mobile``.
        # The gateway scheduler reads this env var for the default device id
        # (set ``MOBILE_HOME_CHANNEL=<device_id>``); explicit ``mobile:<id>``
        # send_message targets work without it. A device id is the chat_id here —
        # see ``MobileAdapter.send`` and ``hermes mobile devices``.
        cron_deliver_env_var="MOBILE_HOME_CHANNEL",
        platform_hint=(
            "You are sending to the user's Hermes mobile app inbox. "
            "Messages are delivered to an in-app mailbox (markdown is "
            "rendered by the app) and announced with a redacted push "
            "notification. Keep messages self-contained — the user may "
            "read them later."
        ),
    )
