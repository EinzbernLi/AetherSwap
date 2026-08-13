from __future__ import annotations

import pytest

from app.auto_offer.counterparty_evidence import (
    CounterpartyEvidenceError,
    SellerCounterpartyEvidence,
    seller_counterparty_from_exact_buff_record,
)


def test_exact_single_seller_field_is_accepted():
    assert seller_counterparty_from_exact_buff_record(
        {"seller_steam_id": "76561198000000001"}
    ) == SellerCounterpartyEvidence("76561198000000001")


def test_exact_alias_field_is_accepted_without_fuzzy_lookup():
    assert seller_counterparty_from_exact_buff_record(
        {"seller_steamid": "76561198000000001"}
    ) == SellerCounterpartyEvidence("76561198000000001")


def test_two_exact_fields_must_agree():
    evidence = seller_counterparty_from_exact_buff_record(
        {
            "seller_steam_id": "76561198000000001",
            "seller_steamid": "76561198000000001",
        }
    )
    assert evidence.steam_id == "76561198000000001"


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"seller": "76561198000000001"},
        {"seller_steam_id": None},
        {"seller_steam_id": ""},
        {"seller_steam_id": " 76561198000000001"},
        {"seller_steam_id": "76561198000000001 "},
        {
            "seller_steam_id": "76561198000000001",
            "seller_steamid": "76561198000000002",
        },
    ],
)
def test_missing_malformed_or_conflicting_seller_identity_fails_closed(record):
    with pytest.raises(CounterpartyEvidenceError):
        seller_counterparty_from_exact_buff_record(record)


def test_non_mapping_record_fails_closed():
    with pytest.raises(CounterpartyEvidenceError):
        seller_counterparty_from_exact_buff_record([])  # type: ignore[arg-type]
