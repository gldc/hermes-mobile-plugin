"""MobileDeviceProvider — per-device dashboard auth for the mobile app.

A :class:`hermes_cli.dashboard_auth.DashboardAuthProvider` backed by the
plugin's :class:`~hermes_mobile.device_store.DeviceStore`. There is no
browser login flow: devices are paired out-of-band by ``hermes mobile
pair`` on the gateway host, which mints a refresh token and renders it in
a QR code. The app then bootstraps a full session by sending a request
carrying only the ``hermes_session_rt`` cookie — the auth middleware
skips verification when no access token is present and calls this
provider's :meth:`refresh_session` directly (middleware.py:209/342), so
``refresh_session`` *is* the device-login endpoint.

Failure semantics follow the ABC exactly:
- ``verify_session`` returns ``None`` for any token it does not
  recognise (providers stack — never raise on foreign tokens).
- ``refresh_session`` raises ``RefreshExpiredError`` for unknown,
  expired, revoked, or reused refresh tokens (reuse additionally revokes
  the device — see device_store) and ``ProviderError`` on store I/O
  failure.
- ``revoke_session`` is best-effort and never raises.
- ``start_login`` redirects to the project docs: the provider's entry on
  the browser login page is informative ("pair via ``hermes mobile
  pair``"), not a usable OAuth flow, so ``complete_login`` raises
  ``InvalidCodeError`` unconditionally.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCodeError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)

from .device_store import DeviceStore, RefreshTokenError

logger = logging.getLogger(__name__)

#: Where ``start_login`` sends a browser that picks "Mobile Device" on the
#: login page — pairing instructions, not an IDP.
PAIRING_DOCS_URL = "https://github.com/gldc/hermes-mobile-plugin"

PROVIDER_NAME = "mobile-device"


class MobileDeviceProvider(DashboardAuthProvider):
    """Device-token auth provider for QR-paired mobile clients."""

    name = PROVIDER_NAME
    display_name = "Mobile Device"
    supports_password = False

    def __init__(self, store: Optional[DeviceStore] = None) -> None:
        self._store = store if store is not None else DeviceStore()

    # ---- browser login surface (informative only) --------------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        # Devices pair out-of-band (`hermes mobile pair` on the gateway
        # host). Pointing at a plugin route is impossible: those sit
        # behind session auth and the login-page visitor is
        # unauthenticated by definition. Redirect to the project README.
        _ = redirect_uri
        return LoginStart(redirect_url=PAIRING_DOCS_URL, cookie_payload={})

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise InvalidCodeError(
            "mobile-device has no browser login flow; pair the device with "
            "`hermes mobile pair` instead"
        )

    # ---- session lifecycle --------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        try:
            record = self._store.verify_access(access_token)
        except Exception:
            # Providers stack: a token minted by another provider (or a
            # broken store) must yield None here, never an exception.
            logger.debug("mobile-device: verify_access failed", exc_info=True)
            return None
        if record is None:
            return None
        return self._session_from_record(
            record,
            access_token=access_token,
            refresh_token="",
            expires_at=int(record.get("access_expires_at", 0)),
        )

    def refresh_session(self, *, refresh_token: str) -> Session:
        try:
            access_token, new_refresh, expires_at = self._store.rotate_refresh(
                refresh_token
            )
        except RefreshTokenError as exc:
            # Unknown / expired / revoked / reused (reuse has already
            # revoked the device inside the store) → force re-pairing.
            raise RefreshExpiredError(str(exc)) from exc
        except Exception as exc:  # store I/O failure — transient, not auth
            raise ProviderError(f"mobile device store unavailable: {exc}") from exc

        record = self._store.verify_access(access_token)
        if record is None:  # pragma: no cover — rotate just minted it
            raise ProviderError("mobile device store inconsistent after rotation")
        return self._session_from_record(
            record,
            access_token=access_token,
            refresh_token=new_refresh,
            expires_at=expires_at,
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        try:
            found = self._store.revoke_by_refresh(refresh_token)
            if found:
                logger.info("mobile-device: device revoked via logout")
        except Exception:
            # Best-effort — must not raise.
            logger.debug("mobile-device: revoke_session failed", exc_info=True)

    # ---- internals -----------------------------------------------------------

    def _session_from_record(
        self,
        record: Dict[str, Any],
        *,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> Session:
        device_id = str(record.get("device_id", ""))
        device_name = str(record.get("name", "")) or device_id
        return Session(
            user_id=f"mobile:{device_id}",
            email=device_name,
            display_name=device_name,
            org_id="",
            provider=self.name,
            expires_at=expires_at,
            access_token=access_token,
            refresh_token=refresh_token,
        )
