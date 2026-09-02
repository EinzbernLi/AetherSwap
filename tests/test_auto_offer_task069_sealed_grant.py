from __future__ import annotations

import pytest

import app.auto_offer.host_integration as host_integration
import app.auto_offer.platform_readonly as platform_readonly
from app.auto_offer.adapters import PlatformAdapterProtocolError


CURRENT_ACCOUNT = "registry-current"
HISTORICAL_ACCOUNT = "deployment-local-old"
STEAM_ID = "76561198000000001"


class _BuffReader:
    def __init__(self):
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
        raise AssertionError("reader must not be called during lineage validation")


def test_direct_recovery_lineage_construction_is_rejected():
    with pytest.raises(PlatformAdapterProtocolError, match="cannot be constructed"):
        platform_readonly._RecoveryAccountLineage(
            CURRENT_ACCOUNT,
            frozenset({CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}),
        )


def test_object_new_forgery_has_no_bearer_authority():
    forged = object.__new__(platform_readonly._RecoveryAccountLineage)
    object.__setattr__(forged, "current_account_id", CURRENT_ACCOUNT)
    object.__setattr__(
        forged,
        "accepted_account_ids",
        frozenset({CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}),
    )
    reader = _BuffReader()

    with pytest.raises(PlatformAdapterProtocolError, match="recovery-builder-only"):
        platform_readonly.BuffReadOnlyAdapter(
            reader,
            account_id=CURRENT_ACCOUNT,
            recovery_lineage=forged,
        )

    assert reader.calls == 0


def test_recovery_lineage_surface_has_no_closure_bearer_secret():
    assert platform_readonly._RecoveryAccountLineage.__new__.__closure__ is None
    assert platform_readonly._make_recovery_account_lineage.__closure__ is None
    assert platform_readonly._accepted_account_ids_for.__closure__ is None


def test_direct_factory_call_remains_recovery_only():
    with pytest.raises(PlatformAdapterProtocolError, match="factory is recovery-only"):
        platform_readonly._make_recovery_account_lineage(
            CURRENT_ACCOUNT,
            frozenset({HISTORICAL_ACCOUNT}),
        )


def test_direct_private_recovery_bridge_call_cannot_bypass_public_identity_admission(
    monkeypatch,
):
    calls = {"store_closed": 0, "session_closed": 0}

    class FakeSession:
        verify = True

        def close(self):
            calls["session_closed"] += 1

    class FakeStore:
        def __init__(self, _path):
            pass

        def initialize_existing(self):
            pass

        def list_recoverable(self):
            return []

        def close(self):
            calls["store_closed"] += 1

    monkeypatch.setattr(host_integration, "SteamHostEgressSession", FakeSession)
    monkeypatch.setattr(host_integration, "AutoOfferStore", FakeStore)

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="recovery_only_bridge_build_failed",
    ):
        host_integration._build_recovery_only_host_auto_offer_bridge(
            buff_client=_BuffReader(),
            account_id=CURRENT_ACCOUNT,
            account_steam_id=STEAM_ID,
            steam_cookie_string="steamLoginSecure=fake",
            store_path="unused.db",
        )

    assert calls == {"store_closed": 1, "session_closed": 1}
