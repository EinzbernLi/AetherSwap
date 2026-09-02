from __future__ import annotations

import pytest

import app.auto_offer.platform_readonly as platform_readonly
from app.auto_offer.adapters import PlatformAdapterProtocolError


CURRENT_ACCOUNT = "registry-current"
HISTORICAL_ACCOUNT = "deployment-local-old"


class _BuffReader:
    def get_steam_trades(self):
        raise AssertionError("reader must not be called during grant validation")


def test_forged_recovery_grant_cannot_bypass_constructor_seal():
    forged = object.__new__(platform_readonly._RecoveryAccountLineage)
    object.__setattr__(forged, "current_account_id", CURRENT_ACCOUNT)
    object.__setattr__(
        forged,
        "accepted_account_ids",
        frozenset({CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}),
    )

    with pytest.raises(PlatformAdapterProtocolError, match="grant is invalid"):
        platform_readonly.BuffReadOnlyAdapter(
            _BuffReader(),
            account_id=CURRENT_ACCOUNT,
            recovery_lineage=forged,
        )


def test_direct_factory_call_remains_recovery_only():
    with pytest.raises(PlatformAdapterProtocolError, match="factory is recovery-only"):
        platform_readonly._make_recovery_account_lineage(
            CURRENT_ACCOUNT,
            frozenset({HISTORICAL_ACCOUNT}),
        )
