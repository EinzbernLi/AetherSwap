from __future__ import annotations

import pytest

import app.auto_offer.platform_readonly as platform_readonly
from app.auto_offer.adapters import PlatformAdapterProtocolError


CURRENT_ACCOUNT = "registry-current"
HISTORICAL_ACCOUNT = "deployment-local-old"


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
