"""Work around an upstream hermes-core dashboard-auth bug.

``hermes_cli.dashboard_auth.middleware._attempt_refresh`` loops over the
registered auth providers calling ``refresh_session`` and **stops at the
first provider that raises ``RefreshExpiredError``**, on the assumption
that "a refresh token belongs to exactly one provider". But a provider
raises ``RefreshExpiredError`` for *both* "this token isn't mine" and
"mine but dead" — the two are indistinguishable at that layer. So when
the ``basic`` provider (username/password dashboard login) is enabled
alongside this plugin's ``mobile-device`` provider and is tried first, it
raises ``RefreshExpiredError`` on a mobile refresh token it doesn't own,
the loop returns, and ``mobile-device`` is never reached. The mobile app
then sees "this device's pairing was revoked or expired" on every pair —
even for a freshly-minted, active device. (It worked before basic-auth
was enabled, because ``mobile-device`` was then the only provider.)

``install()`` replaces ``_attempt_refresh`` with an all-providers variant
that keeps trying after a ``RefreshExpiredError`` and only gives up when
no provider accepts the token. This is safe both ways: a basic token is
declined by ``mobile-device`` with no side effect (an unknown token isn't
in the device store, so nothing is mutated), and a mobile token is
declined by ``basic`` with no side effect.

Idempotent and defensive: a guard flag prevents double-patching, every
core symbol the replacement needs is checked first, and any failure is
logged and left non-fatal rather than breaking plugin registration. The
replacement resolves all core names off the live middleware module at
call time, so it tracks core's own ``list_providers`` / ``audit_log`` /
etc. Remove once the upstream fix lands (``return None`` -> ``continue``
in the ``except RefreshExpiredError`` handler).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Set on the middleware module once patched, so a second ``install()``
#: (register runs once per host process) is a no-op.
_PATCH_FLAG = "_hermes_mobile_allproviders_refresh"

#: Core symbols the replacement reads off the middleware module. If any is
#: absent (core refactor), we decline to patch rather than install a broken
#: function.
_REQUIRED = (
    "_attempt_refresh",
    "list_providers",
    "RefreshExpiredError",
    "ProviderError",
    "audit_log",
    "AuditEvent",
    "_client_ip",
    "_log",
)


def install() -> bool:
    """Patch ``_attempt_refresh`` to try every provider.

    Returns ``True`` if the patch is in place afterwards (already-patched
    counts as success), ``False`` if it could not be applied (logged,
    non-fatal — e.g. a non-dashboard host process where the middleware
    isn't importable, or a core refactor that removed a needed symbol).
    """
    try:
        from hermes_cli.dashboard_auth import middleware as mw
    except Exception:
        # CLI / gateway process (no dashboard middleware) — nothing to do.
        logger.debug(
            "hermes-mobile: dashboard middleware not importable; "
            "skipping _attempt_refresh patch",
            exc_info=True,
        )
        return False

    if getattr(mw, _PATCH_FLAG, False):
        return True

    missing = [name for name in _REQUIRED if not hasattr(mw, name)]
    if missing:
        logger.warning(
            "hermes-mobile: not patching _attempt_refresh; core symbols "
            "missing (%s) — upstream may have changed. Mobile token refresh "
            "may be shadowed by another auth provider.",
            ", ".join(missing),
        )
        return False

    def _attempt_refresh_all_providers(request, *, refresh_token):
        # All names resolved off `mw` at call time so this tracks core.
        if not refresh_token:
            return None
        last_expired_provider = None
        for provider in mw.list_providers():
            try:
                new_session = provider.refresh_session(refresh_token=refresh_token)
            except mw.RefreshExpiredError:
                # Don't stop: RefreshExpiredError can mean "not my token",
                # so the RT may belong to a provider later in the list
                # (this plugin's `mobile-device`). Remember and keep going.
                last_expired_provider = provider.name
                continue
            except mw.ProviderError as exc:
                # A provider's IDP is unreachable — same as upstream: log and
                # force a clean re-login rather than 500 the request.
                mw._log.warning(
                    "dashboard-auth: provider %r unreachable during refresh: %s",
                    provider.name,
                    exc,
                )
                mw.audit_log(
                    mw.AuditEvent.REFRESH_FAILURE,
                    provider=provider.name,
                    reason="provider_unreachable",
                    ip=mw._client_ip(request),
                )
                return None
            if new_session is not None:
                return new_session, provider.name
        if last_expired_provider is not None:
            mw.audit_log(
                mw.AuditEvent.REFRESH_FAILURE,
                provider=last_expired_provider,
                reason="refresh_expired",
                ip=mw._client_ip(request),
            )
        return None

    mw._attempt_refresh = _attempt_refresh_all_providers
    setattr(mw, _PATCH_FLAG, True)
    logger.info(
        "hermes-mobile: patched dashboard _attempt_refresh to try ALL auth "
        "providers (works around basic-vs-mobile-device refresh short-circuit)"
    )
    return True
