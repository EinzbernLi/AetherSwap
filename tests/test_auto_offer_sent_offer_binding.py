import ast
import importlib
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.auto_offer.adapters import (
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.sent_offer_binding import (
    SentOfferBindingAmbiguousError,
    SentOfferBindingContractError,
    SentOfferBindingEvidence,
    SentOfferDiscoveryEvidence,
    SentOfferDiscoveryQuery,
    close_exact_sent_offer_candidate,
    select_unique_sent_offer_candidate,
)


RECIPIENT = "76561198000000001"
COUNTERPARTY = "76561198000000002"


def query(**changes):
    value = SentOfferDiscoveryQuery(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id=RECIPIENT,
        revision=7,
        offer_attempted_at=1234.5,
    )
    return replace(value, **changes)


def discovery(candidate_ids=(), **query_changes):
    return SentOfferDiscoveryEvidence(
        query=query(**query_changes),
        candidate_tradeoffer_ids=tuple(candidate_ids),
    )


def item(assetid="asset-1"):
    return TradeOfferItemEvidence(
        appid=730,
        contextid="2",
        assetid=assetid,
        amount=1,
    )


def exact_offer(**changes):
    value = SteamTradeOfferEvidence(
        steam_tradeoffer_id="offer-1",
        account_steam_id=RECIPIENT,
        counterparty_steam_id=COUNTERPARTY,
        is_our_offer=True,
        lifecycle=SteamTradeOfferLifecycle.ACTIVE,
        items_to_give=(),
        items_to_receive=(item(),),
    )
    return replace(value, **changes)


def test_module_is_pure_and_contains_no_wire_or_time_window_assumptions():
    module = importlib.import_module("app.auto_offer.sent_offer_binding")
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported.isdisjoint(
        {
            "aiohttp",
            "buff",
            "httpx",
            "requests",
            "socket",
            "sqlite3",
            "steam",
            "threading",
            "time",
        }
    )
    assert "HISTORY_TIME_TOLERANCE_SECONDS" not in source
    assert "time_created" not in source
    assert "trade_offers_sent" not in source
    assert "300" not in source
    assert "AutoOfferStore" not in source
    assert "sleep(" not in source


def test_query_is_exact_immutable_and_canonicalizes_attempt_timestamp():
    value = SentOfferDiscoveryQuery(
        "purchase-1",
        "buff-order-1",
        "account-1",
        RECIPIENT,
        1,
        5,
    )

    assert value.offer_attempted_at == 5.0
    with pytest.raises(FrozenInstanceError):
        value.revision = 2


@pytest.mark.parametrize(
    "changes",
    [
        {"purchase_id": ""},
        {"buff_order_id": " buff-order-1"},
        {"account_id": "account-1 "},
        {"recipient_steam_id": "steam-1"},
        {"recipient_steam_id": "076561198000000001"},
        {"revision": 0},
        {"revision": True},
        {"offer_attempted_at": -1},
        {"offer_attempted_at": math.inf},
        {"offer_attempted_at": math.nan},
        {"offer_attempted_at": True},
    ],
)
def test_query_rejects_noncanonical_local_identity_or_attempt_anchor(changes):
    with pytest.raises(SentOfferBindingContractError):
        query(**changes)


def test_discovery_candidate_order_is_non_authoritative_and_canonical():
    value = discovery(("offer-2", "offer-1"))

    assert value.candidate_tradeoffer_ids == ("offer-1", "offer-2")
    with pytest.raises(FrozenInstanceError):
        value.candidate_tradeoffer_ids = ()


@pytest.mark.parametrize(
    "candidate_ids",
    [
        ["offer-1"],
        ("",),
        (" offer-1",),
        ("offer-1", "offer-1"),
    ],
)
def test_discovery_rejects_non_tuple_malformed_or_duplicate_candidates(candidate_ids):
    with pytest.raises(SentOfferBindingContractError):
        SentOfferDiscoveryEvidence(
            query=query(),
            candidate_tradeoffer_ids=candidate_ids,
        )


def test_zero_candidate_returns_none_without_inventing_identity():
    assert select_unique_sent_offer_candidate(discovery()) is None


def test_one_candidate_returns_only_exact_candidate():
    assert select_unique_sent_offer_candidate(discovery(("offer-1",))) == "offer-1"


def test_two_or_more_candidates_fail_closed_without_latest_selection():
    with pytest.raises(
        SentOfferBindingAmbiguousError,
        match="ambiguous_sent_offer_candidates",
    ):
        select_unique_sent_offer_candidate(discovery(("offer-2", "offer-1")))


@pytest.mark.parametrize(
    "lifecycle",
    [
        SteamTradeOfferLifecycle.ACTIVE,
        SteamTradeOfferLifecycle.ACCEPTED,
        SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
        SteamTradeOfferLifecycle.IN_ESCROW,
        SteamTradeOfferLifecycle.CANCELED,
    ],
)
def test_exact_closure_binds_identity_without_inventing_lifecycle_policy(lifecycle):
    result = close_exact_sent_offer_candidate(
        discovery(("offer-1",)),
        exact_offer(lifecycle=lifecycle),
    )

    assert result == SentOfferBindingEvidence(
        query=query(),
        steam_tradeoffer_id="offer-1",
        counterparty_steam_id=COUNTERPARTY,
    )
    assert not hasattr(result, "lifecycle")


@pytest.mark.parametrize(
    "offer_changes,expected",
    [
        ({"steam_tradeoffer_id": "offer-2"}, "tradeoffer_identity_mismatch"),
        ({"account_steam_id": "76561198000000003"}, "account_identity_mismatch"),
        ({"is_our_offer": False}, "buyer_offer_direction_mismatch"),
        ({"counterparty_steam_id": "seller"}, "counterparty_identity_mismatch"),
        ({"items_to_give": (item("give-1"),)}, "buyer_offer_gives_items"),
        (
            {
                "items_to_give": (item("give-1"),),
                "items_to_receive": (),
            },
            "buyer_offer_gives_items",
        ),
    ],
)
def test_exact_closure_rejects_identity_direction_or_item_side_mismatch(
    offer_changes,
    expected,
):
    with pytest.raises(SentOfferBindingContractError, match=expected):
        close_exact_sent_offer_candidate(
            discovery(("offer-1",)),
            exact_offer(**offer_changes),
        )


def test_exact_closure_requires_unique_candidate_before_exact_offer_can_bind():
    with pytest.raises(
        SentOfferBindingContractError,
        match="sent_offer_candidate_not_found",
    ):
        close_exact_sent_offer_candidate(discovery(), exact_offer())

    with pytest.raises(SentOfferBindingAmbiguousError):
        close_exact_sent_offer_candidate(
            discovery(("offer-1", "offer-2")),
            exact_offer(),
        )


def test_binding_evidence_rejects_cross_account_counterparty_identity():
    with pytest.raises(SentOfferBindingContractError):
        SentOfferBindingEvidence(
            query=query(),
            steam_tradeoffer_id="offer-1",
            counterparty_steam_id=RECIPIENT,
        )


def test_exact_closure_does_not_mutate_discovery_or_exact_offer():
    found = discovery(("offer-1",))
    offer = exact_offer()
    before_found = found
    before_offer = offer

    result = close_exact_sent_offer_candidate(found, offer)

    assert found == before_found
    assert offer == before_offer
    assert result.query is found.query
