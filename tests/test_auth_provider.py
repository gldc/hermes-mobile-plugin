"""Tests for hermes_mobile.auth_provider.MobileDeviceProvider.

Requires hermes on the import path:
    PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -x -q
"""

from __future__ import annotations

import pytest

from hermes_cli.dashboard_auth import (
    InvalidCodeError,
    RefreshExpiredError,
    Session,
    assert_protocol_compliance,
)

from hermes_mobile.auth_provider import PAIRING_DOCS_URL, MobileDeviceProvider
from hermes_mobile.device_store import (
    ACCESS_TTL_SECONDS,
    DeviceStore,
)


class FakeClock:
    def __init__(self, t: int = 1_750_000_000) -> None:
        self.t = t

    def __call__(self) -> float:
        return float(self.t)

    def advance(self, seconds: int) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json", clock=clock)


@pytest.fixture
def provider(store) -> MobileDeviceProvider:
    return MobileDeviceProvider(store=store)


@pytest.fixture
def paired(store):
    """A freshly paired device: (device_id, refresh_token)."""
    return store.create_device("Gianluca's iPhone")


# ---------------------------------------------------------------------------
# Protocol compliance (hermes' prescribed provider test)
# ---------------------------------------------------------------------------


def test_protocol_compliance():
    assert_protocol_compliance(MobileDeviceProvider)


def test_provider_identity():
    assert MobileDeviceProvider.name == "mobile-device"
    assert MobileDeviceProvider.display_name
    assert MobileDeviceProvider.supports_password is False


# ---------------------------------------------------------------------------
# start_login / complete_login
# ---------------------------------------------------------------------------


def test_start_login_redirects_to_project_docs(provider):
    start = provider.start_login(redirect_uri="https://gw.tailnet/auth/callback")
    assert start.redirect_url == PAIRING_DOCS_URL
    assert start.redirect_url.startswith("https://github.com/")
    assert start.cookie_payload == {}


def test_complete_login_raises_invalid_code(provider):
    with pytest.raises(InvalidCodeError):
        provider.complete_login(
            code="x", state="y", code_verifier="z", redirect_uri="https://gw/cb"
        )


# ---------------------------------------------------------------------------
# refresh_session — the QR bootstrap / rotation path
# ---------------------------------------------------------------------------


def test_refresh_session_mints_full_session(provider, paired, clock):
    device_id, rt = paired
    session = provider.refresh_session(refresh_token=rt)
    assert isinstance(session, Session)
    assert session.user_id == f"mobile:{device_id}"
    assert session.provider == "mobile-device"
    assert session.org_id == ""
    assert session.email == "Gianluca's iPhone"
    assert session.display_name == "Gianluca's iPhone"
    assert session.expires_at == int(clock.t) + ACCESS_TTL_SECONDS
    assert session.access_token
    assert session.refresh_token
    assert session.refresh_token != rt  # rotated


def test_refresh_session_unknown_rt_raises_refresh_expired(provider):
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token="never-issued")


def test_refresh_session_empty_rt_raises_refresh_expired(provider):
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token="")


def test_refresh_session_revoked_device_raises(provider, store, paired):
    device_id, rt = paired
    store.revoke(device_id)
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=rt)


def test_refresh_session_reuse_revokes_and_raises(provider, store, paired):
    _, rt1 = paired
    s1 = provider.refresh_session(refresh_token=rt1)  # rt1 -> rt2
    provider.refresh_session(
        refresh_token=s1.refresh_token
    )  # rt2 -> rt3; rt1 two behind
    # Replaying a rotated-out RT older than the immediate prior = reuse → revoke.
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=rt1)
    # ... and the whole device is dead, including the current RT.
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=s1.refresh_token)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True


# ---------------------------------------------------------------------------
# verify_session
# ---------------------------------------------------------------------------


def test_verify_session_roundtrip(provider, paired):
    device_id, rt = paired
    minted = provider.refresh_session(refresh_token=rt)
    session = provider.verify_session(access_token=minted.access_token)
    assert session is not None
    assert session.user_id == f"mobile:{device_id}"
    assert session.provider == "mobile-device"
    assert session.access_token == minted.access_token
    assert session.expires_at == minted.expires_at


def test_verify_session_unknown_token_returns_none(provider):
    # MUST NOT raise — providers stack; other providers' tokens flow through.
    assert provider.verify_session(access_token="some-other-providers-token") is None
    assert provider.verify_session(access_token="") is None


def test_verify_session_expired_token_returns_none(provider, paired, clock):
    _, rt = paired
    minted = provider.refresh_session(refresh_token=rt)
    clock.advance(ACCESS_TTL_SECONDS + 1)
    assert provider.verify_session(access_token=minted.access_token) is None


def test_verify_session_never_raises_even_on_store_corruption(tmp_path, clock):
    path = tmp_path / "devices.json"
    path.write_text("{ not json !")
    provider = MobileDeviceProvider(store=DeviceStore(path=path, clock=clock))
    assert provider.verify_session(access_token="anything") is None


# ---------------------------------------------------------------------------
# revoke_session
# ---------------------------------------------------------------------------


def test_revoke_session_revokes_device(provider, store, paired):
    device_id, rt = paired
    provider.revoke_session(refresh_token=rt)
    (dev,) = store.list_devices()
    assert dev["revoked"] is True
    with pytest.raises(RefreshExpiredError):
        provider.refresh_session(refresh_token=rt)


def test_revoke_session_is_best_effort_and_never_raises(provider, tmp_path, clock):
    provider.revoke_session(refresh_token="unknown-token")  # no raise
    provider.revoke_session(refresh_token="")  # no raise
    path = tmp_path / "corrupt.json"
    path.write_text("not json")
    broken = MobileDeviceProvider(store=DeviceStore(path=path, clock=clock))
    broken.revoke_session(refresh_token="x")  # no raise even on corruption
