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

    # 2. CLI commands (`hermes mobile pair|devices|revoke`) — implemented
    #    in hermes_mobile.cli (later milestone). Import structure is in
    #    place; we register only what exists now.
    _register_cli(ctx, store)

    # 3. Platform adapter ('mobile' push/mailbox platform) — later
    #    milestone; will live in hermes_mobile.platform_adapter and be
    #    registered here via ctx.register_platform(...).


def _register_cli(ctx, store: DeviceStore) -> None:
    try:
        from . import cli  # type: ignore[attr-defined]
    except ImportError:
        logger.debug(
            "hermes-mobile: CLI module not present yet; skipping register_cli_command"
        )
        return
    cli.register_cli(ctx, store)
