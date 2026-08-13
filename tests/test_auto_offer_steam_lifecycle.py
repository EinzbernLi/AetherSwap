from __future__ import annotations

import pytest

from app.auto_offer.adapters import SteamTradeOfferLifecycle
from app.auto_offer.steam_lifecycle import (
    SteamLifecycleEvidenceError,
    map_exact_steam_lifecycle,
)


@pytest.mark.parametrize(
    ("raw", "expected", "terminal"),
    [
        ("active", SteamTradeOfferLifecycle.ACTIVE, False),
        ("accepted", SteamTradeOfferLifecycle.ACCEPTED, False),
        (
            "created_needs_confirmation",
            SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
            False,
        ),
        ("countered", SteamTradeOfferLifecycle.COUNTERED, True),
        ("expired", SteamTradeOfferLifecycle.EXPIRED, True),
        ("canceled", SteamTradeOfferLifecycle.CANCELED, True),
        ("declined", SteamTradeOfferLifecycle.DECLINED, True),
        ("invalid_items", SteamTradeOfferLifecycle.INVALID_ITEMS, True),
        (
            "canceled_by_second_factor",
            SteamTradeOfferLifecycle.CANCELED_BY_SECOND_FACTOR,
            True,
        ),
        ("in_escrow", SteamTradeOfferLifecycle.IN_ESCROW, False),
    ],
)
def test_exact_lifecycle_mapping(raw, expected, terminal):
    evidence = map_exact_steam_lifecycle(raw)
    assert evidence.lifecycle is expected
    assert evidence.detail == f"trade_offer_{expected.value}"
    assert evidence.terminal_without_trade is terminal


@pytest.mark.parametrize(
    "raw",
    [None, "", " active", "active ", "cancelled", "unknown", 3],
)
def test_unknown_or_noncanonical_lifecycle_fails_closed(raw):
    with pytest.raises(
        SteamLifecycleEvidenceError,
        match="trade_offer_state_not_proven",
    ):
        map_exact_steam_lifecycle(raw)
