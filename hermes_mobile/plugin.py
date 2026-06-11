"""Plugin registration logic — called from the repo-root ``__init__.py``.

Kept inside the ``hermes_mobile`` package so it is unit-testable with a
plain ``import hermes_mobile.plugin`` (the root ``__init__.py`` is only
importable through hermes' directory-plugin loader).
"""

from __future__ import annotations

import logging
from typing import Optional

from .auth_provider import MobileDeviceProvider
from .device_store import DeviceStore

logger = logging.getLogger(__name__)


def register_all(ctx, store: Optional[DeviceStore] = None) -> None:
    """Register every surface the plugin currently implements.

    The same ``register(ctx)`` runs in the CLI, the gateway, and the
    dashboard web-server process, so this must stay cheap and safe when
    only a subset of host surfaces is present.
    """
    if store is None:
        store = DeviceStore()  # ~/.hermes/mobile/devices.json

    # 1. Dashboard auth provider — device tokens for the mobile app.
    #    No configuration is required: an empty device store simply means
    #    no mobile token ever verifies, so we always register (the
    #    register hook itself warn-and-ignores misbehaving providers).
    provider = MobileDeviceProvider(store=store)
    ctx.register_dashboard_auth_provider(provider)
    logger.info("hermes-mobile: registered '%s' auth provider", provider.name)

    # 2. CLI commands (`hermes mobile pair|devices|revoke`).
    _register_cli(ctx, store)

    # 3. Platform adapter — the 'mobile' mailbox + redacted-Expo-push
    #    platform. hermes_mobile.adapter imports gateway code, so it is
    #    only imported here, at registration time, where the host
    #    guarantees the gateway package is importable.
    _register_platform(ctx, store)


def _register_cli(ctx, store: DeviceStore) -> None:
    try:
        from . import cli  # type: ignore[attr-defined]
    except ImportError:
        logger.debug(
            "hermes-mobile: CLI module not present yet; skipping register_cli_command"
        )
        return
    cli.register_cli(ctx, store)


def _register_platform(ctx, store: DeviceStore) -> None:
    try:
        from . import adapter
    except ImportError:
        # Host process without the gateway package on the path — the
        # auth provider and CLI must keep working regardless.
        logger.debug(
            "hermes-mobile: gateway not importable; skipping register_platform",
            exc_info=True,
        )
        return
    adapter.register_platform(ctx, store)
    logger.info("hermes-mobile: registered 'mobile' platform adapter")
