from __future__ import annotations

import pytest

from app.auto_offer.buff_order_evidence import (
    BuffOrderEvidenceError,
    ExactSellerBuffItemEvidence,
    authorize_exact_seller_accept,
    normalize_exact_seller_buff_item,
)
from app.auto_offer.adapters import (
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)


def record(**changes):
    value = {
        "buff_order_id": "order-1",
        "tradeofferid": "offer-1",
        "buyer_steam_id": "recipient-1",
        "seller_steam_id": "seller-1",
        "items_to_trade": [{"assetid": "asset-1", "goods_id": 73001}],
        "goods_infos": {
            "73001": {
                "name": "display-only",
                "market_hash_name": "display-only",
            }
        },
    }
    value.update(changes)
    return value


def normalize(records, **changes):
    values = {
        "buff_order_id": "order-1",
        "recipient_steam_id": "recipient-1",
        "host_goods_id": 73001,
    }
    values.update(changes)
    return normalize_exact_seller_buff_item(records, **values)


def steam_evidence(**changes):
    values = {
        "steam_tradeoffer_id": "offer-1",
        "account_steam_id": "recipient-1",
        "counterparty_steam_id": "seller-1",
        "is_our_offer": False,
        "lifecycle": SteamTradeOfferLifecycle.ACTIVE,
        "items_to_give": (),
        "items_to_receive": (TradeOfferItemEvidence(730, "2", "asset-1", 1),),
    }
    values.update(changes)
    return SteamTradeOfferEvidence(**values)


def test_unique_one_order_one_item_normalizes_exact_authorization_evidence():
    assert normalize([record()]) == ExactSellerBuffItemEvidence(
        buff_order_id="order-1",
        steam_tradeoffer_id="offer-1",
        recipient_steam_id="recipient-1",
        counterparty_steam_id="seller-1",
        goods_id=73001,
        seller_assetid="asset-1",
    )


def test_exact_buff_and_incoming_steam_item_authorize_one_accept():
    buff = normalize([record()])
    assert authorize_exact_seller_accept(buff, steam_evidence()) is buff


@pytest.mark.parametrize(
    ("steam", "reason"),
    [
        (steam_evidence(steam_tradeoffer_id="offer-2"), "tradeoffer_id_mismatch"),
        (steam_evidence(account_steam_id="recipient-2"), "recipient_steam_id_mismatch"),
        (steam_evidence(counterparty_steam_id="seller-2"), "seller_steam_id_mismatch"),
        (steam_evidence(is_our_offer=True), "incoming_offer_required"),
        (
            steam_evidence(lifecycle=SteamTradeOfferLifecycle.DECLINED),
            "active_offer_required",
        ),
        (
            steam_evidence(
                items_to_give=(TradeOfferItemEvidence(730, "2", "give-1", 1),)
            ),
            "outgoing_items_present",
        ),
        (
            steam_evidence(
                items_to_receive=(
                    TradeOfferItemEvidence(730, "2", "asset-1", 1),
                    TradeOfferItemEvidence(730, "2", "asset-2", 1),
                )
            ),
            "steam_item_mapping_not_unique",
        ),
        (
            steam_evidence(
                items_to_receive=(TradeOfferItemEvidence(440, "2", "asset-1", 1),)
            ),
            "cs2_item_identity_mismatch",
        ),
        (
            steam_evidence(
                items_to_receive=(TradeOfferItemEvidence(730, "2", "asset-2", 1),)
            ),
            "seller_assetid_mismatch",
        ),
    ],
)
def test_accept_authorization_fails_closed_on_any_identity_or_cardinality_gap(
    steam, reason
):
    with pytest.raises(BuffOrderEvidenceError, match=f"^{reason}$"):
        authorize_exact_seller_accept(normalize([record()]), steam)


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ([], "order_mapping_not_unique"),
        ([record(), record(tradeofferid="offer-2")], "order_mapping_not_unique"),
        (
            [record(), record(buff_order_id="order-2")],
            "aggregated_offer_not_supported",
        ),
        (
            [
                record(
                    items_to_trade=[
                        {"assetid": "asset-1", "goods_id": 73001},
                        {"assetid": "asset-2", "goods_id": 73001},
                    ]
                )
            ],
            "item_mapping_not_unique",
        ),
        (
            [record(items_to_trade=[])],
            "item_mapping_not_unique",
        ),
    ],
)
def test_ambiguous_order_offer_or_item_cardinality_fails_closed(records, reason):
    with pytest.raises(BuffOrderEvidenceError, match=f"^{reason}$"):
        normalize(records)


@pytest.mark.parametrize(
    ("records", "kwargs", "reason"),
    [
        (
            [record(trade_offer_id="conflicting-offer")],
            {},
            "tradeoffer_not_proven",
        ),
        (
            [record(buyer_steam_id="other-recipient")],
            {},
            "recipient_steam_id_mismatch",
        ),
        (
            [record(seller_steam_id="recipient-1")],
            {},
            "self_counterparty",
        ),
        (
            [record(items_to_trade=[{"assetid": "asset-1", "goods_id": 73002}])],
            {},
            "goods_id_mismatch",
        ),
        (
            [record(items_to_trade=[{"assetid": "", "goods_id": 73001}])],
            {},
            "invalid_seller_assetid",
        ),
    ],
)
def test_required_exact_identifiers_fail_closed(records, kwargs, reason):
    with pytest.raises(BuffOrderEvidenceError, match=f"^{reason}$"):
        normalize(records, **kwargs)


def test_display_names_and_goods_infos_never_substitute_for_exact_item_fields():
    payload = record(
        items_to_trade=[
            {
                "name": "asset-1",
                "market_hash_name": "asset-1",
            }
        ],
        goods_infos={
            "73001": {
                "name": "asset-1",
                "market_hash_name": "asset-1",
            }
        },
    )

    with pytest.raises(BuffOrderEvidenceError, match="^invalid_goods_id$"):
        normalize([payload])
