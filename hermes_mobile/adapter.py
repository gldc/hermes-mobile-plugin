"""MobileAdapter — the 'mobile' gateway platform (mailbox + redacted push).

Registered via ``ctx.register_platform`` (CONTRACTS.md §1.3); makes a
paired phone a first-class cron-delivery / ``hermes send`` target:
``chat_id`` is the device id (``mobile:<device_id>``, parsed and validated by
``parse_target_ref`` / ``make_target_validator``; delivered out of process by
``make_standalone_sender``). ``send()`` appends the message to the
device's mailbox file (``~/.hermes/mobile/mailbox/<device_id>.jsonl``)
and, when the device has registered an Expo push token, fires a
redacted push ("New message from Hermes") — content never transits
Expo/APNs; the app fetches it over the VPN by draining the mailbox.

This module imports gateway code, so it is only imported lazily from
the platform adapter_factory (the auth provider and CLI must keep
working in processes where the gateway package is absent).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple, Union

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .device_store import DEVICE_ID_BYTES, DeviceStore
from .mailbox import append_message, default_mailbox_dir, is_safe_device_id
from .push import ExpoPush

logger = logging.getLogger(__name__)

PLATFORM_NAME = "mobile"

#: A device id exactly as ``DeviceStore.create_device`` mints it (ASCII only:
#: ``[0-9a-f]`` is a literal range, unlike ``\d``).
_DEVICE_ID_RE = re.compile(rf"[0-9a-f]{{{DEVICE_ID_BYTES * 2}}}")


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


# ---- explicit targets (``mobile:<device_id>``) --------------------------------


def parse_target_ref(target_ref: str) -> Optional[Tuple[str, Optional[str]]]:
    """PlatformEntry ``parse_target_ref_fn``: a device id → ``(device_id, None)``.

    Only an exact device id is explicit (case and surrounding whitespace are
    normalised). Anything else returns ``None`` so the core keeps resolving
    (channel directory, then its "could not resolve" error). Names are never
    resolved here: they are not unique. Mobile has no threads.
    """
    if not isinstance(target_ref, str):
        return None
    candidate = target_ref.strip().lower()
    if _DEVICE_ID_RE.fullmatch(candidate):
        return candidate, None
    return None


def make_target_validator(store: DeviceStore) -> Callable[[str], Union[bool, str]]:
    """PlatformEntry ``validate_target_ref_fn``: accept only paired, unrevoked devices.

    Reads the store on every call: ``hermes mobile pair``/``revoke`` and
    ``hermes send`` run in processes other than the gateway.
    """

    def validate_target_ref(chat_id: str) -> Union[bool, str]:
        try:
            device = store.get_device(chat_id)
        except Exception as exc:
            logger.warning("hermes-mobile: device store unreadable: %s", exc)
            return f"cannot read the mobile device store ({exc})"
        if device is None:
            return f"no paired device {chat_id} — see `hermes mobile devices`"
        if device.get("revoked"):
            return f"device {chat_id} is revoked"
        return True

    return validate_target_ref


def _root_mailbox_write_refusal(mailbox_dir: Path) -> Optional[str]:
    """Why root must not write *mailbox_dir*, or ``None`` when it may.

    The gateway (uid 10000 in the image) owns the mailbox tree. ``docker exec …
    hermes`` drops to that uid by default, but ``HERMES_DOCKER_EXEC_AS_ROOT=1``
    keeps root, and a root-created ``<id>.jsonl`` (0600) or ``mailbox/`` (0700)
    would lock the gateway's appends and the dashboard's drain out; root would
    also follow a symlink the gateway user planted there. Root may write only a
    tree that root already owns (nearest existing ancestor).
    """
    if os.geteuid() != 0:
        return None
    probe = Path(mailbox_dir)
    while not os.path.lexists(probe) and probe != probe.parent:
        probe = probe.parent
    try:
        owner = os.lstat(probe).st_uid
    except OSError as exc:
        return (
            f"refusing to write the mobile mailbox as root: cannot stat {probe} ({exc})"
        )
    if owner == 0:
        return None
    return (
        f"refusing to write the mobile mailbox as root: {probe} belongs to uid "
        f"{owner}. Run the command as that user (unset HERMES_DOCKER_EXEC_AS_ROOT)."
    )


def make_standalone_sender(
    store: DeviceStore,
    *,
    push: Optional[ExpoPush] = None,
    mailbox_dir: Optional[Path] = None,
):
    """PlatformEntry ``standalone_sender_fn``: deliver without a live gateway adapter.

    ``hermes send`` and out-of-process cron have no gateway runner, so the core
    calls this instead of the live adapter. Delivery is filesystem + outbound
    HTTPS, so it runs ``MobileAdapter.send`` itself (same mailbox append, same
    redacted push); nothing is duplicated. Mobile has no threads and no media:
    ``thread_id``/``media_files``/``force_document`` are accepted and ignored,
    exactly as the live adapter's text ``send`` ignores them.
    """

    async def standalone_send(
        pconfig,
        chat_id,
        message,
        *,
        thread_id=None,
        media_files=None,
        force_document=False,
    ) -> Dict[str, Any]:
        adapter = MobileAdapter(
            pconfig, store=store, push=push, mailbox_dir=mailbox_dir
        )
        refusal = _root_mailbox_write_refusal(adapter._mailbox_dir)
        if refusal:
            return {"error": refusal}
        result = await adapter.send(str(chat_id), message)
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"error": result.error or "mobile delivery failed"}

    return standalone_send


def _platform_entry_fields() -> FrozenSet[str]:
    """Field names of this core's ``PlatformEntry`` (unknown kwargs raise TypeError)."""
    try:
        from gateway import platform_registry

        return frozenset(
            f.name
            for f in dataclasses.fields(platform_registry.PlatformEntry)
            if f.init  # an init=False field is not a constructor kwarg
        )
    except Exception:
        logger.debug("hermes-mobile: cannot introspect PlatformEntry", exc_info=True)
        return frozenset()


def register_platform(ctx, store: DeviceStore) -> None:
    """Register the 'mobile' platform on the plugin context."""
    # Explicit ``mobile:<device_id>`` targets (``hermes send``, cron
    # ``deliver=mobile:<id>``) and out-of-process delivery. Passed only when this
    # core's PlatformEntry declares the field: register_platform forwards kwargs
    # to the dataclass, so an unknown key would raise TypeError.
    optional = {
        "parse_target_ref_fn": parse_target_ref,
        "validate_target_ref_fn": make_target_validator(store),
        "standalone_sender_fn": make_standalone_sender(store),
    }
    supported = _platform_entry_fields()
    target_kwargs = {k: v for k, v in optional.items() if k in supported}
    base_kwargs = _base_registration_kwargs(store)
    try:
        ctx.register_platform(**target_kwargs, **base_kwargs)
    except TypeError as exc:
        if not target_kwargs:
            raise
        # Detection was wrong for this core (e.g. its register_platform builds a
        # different entry class). Never let that keep the platform from loading:
        # register exactly as before these fields existed.
        logger.warning(
            "hermes-mobile: register_platform rejected %s (%s); retrying without "
            "them — mobile:<device_id> targets and out-of-process sends are off",
            ", ".join(sorted(target_kwargs)),
            exc,
        )
        ctx.register_platform(**base_kwargs)


def _base_registration_kwargs(store: DeviceStore) -> Dict[str, Any]:
    """The registration every supported core accepts (pre-0.2.1 behaviour)."""
    return dict(
        name=PLATFORM_NAME,
        label="Mobile",
        adapter_factory=lambda cfg: MobileAdapter(cfg, store=store),
        check_fn=check_requirements,
        install_hint="No extra packages needed (stdlib only)",
        emoji="📱",
        # Lets cron / scheduled jobs deliver to the phone via ``deliver=mobile``.
        # The gateway scheduler reads this env var for the default device id
        # (set ``MOBILE_HOME_CHANNEL=<device_id>``); explicit ``mobile:<id>``
        # targets (cron ``deliver=mobile:<id>``, ``hermes send -t mobile:<id>``)
        # work without it via ``parse_target_ref_fn``. A device id is the chat_id
        # here — see ``MobileAdapter.send`` and ``hermes mobile devices``.
        cron_deliver_env_var="MOBILE_HOME_CHANNEL",
        platform_hint=(
            "You are sending to the user's Hermes mobile app inbox. "
            "Messages are delivered to an in-app mailbox (markdown is "
            "rendered by the app) and announced with a redacted push "
            "notification. Keep messages self-contained — the user may "
            "read them later."
        ),
    )
