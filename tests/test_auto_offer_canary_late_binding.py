from __future__ import annotations

import pytest

from app.auto_offer.canary_authority import CanaryAuthorityError, CanaryPermit
from app.auto_offer.coordinator import (
    ReadOnlyCoordinatorError,
    _validate_trade_offer_expectations,
)
from app.auto_offer.host_integration import (
    HostAutoOfferIntegrationError,
    preflight_canary_permit,
)
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import StoredDelivery


ORDER = "late-bind-order"
ACCOUNT = "account"
RECIPIENT = "76561198000000001"
SELLER = "76561198000000002"


def _permit(counterparty, direction):
    return CanaryPermit(
        permit_id="permit",
        owner_nonce="nonce",
        host_db_id=1,
        buff_order_id=ORDER,
        purchase_id=f"buff:{ORDER}",
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        expected_counterparty_steam_id=counterparty,
        expected_is_our_offer=direction,
        expected_host_order_ids=(ORDER,),
        expected_store_present=True,
        expected_store_revision=2,
        expected_store_status=DeliveryStatus.AWAITING_OFFER.value,
        expected_store_tradeoffer_id=None,
        created_at=1.0,
    )


def test_buyer_permit_allows_direction_only_before_send():
    permit = _permit(None, True)
    assert permit.expected_counterparty_steam_id is None
    assert permit.expected_is_our_offer is True
    assert _validate_trade_offer_expectations(None, True) == (None, True)


def test_seller_permit_requires_exact_counterparty_before_accept():
    with pytest.raises(CanaryAuthorityError):
        _permit(None, False)
    with pytest.raises(ReadOnlyCoordinatorError):
        _validate_trade_offer_expectations(None, False)
    assert _permit(SELLER, False).expected_counterparty_steam_id == SELLER


def test_preflight_accepts_late_bound_buyer_but_rejects_unbound_seller():
    stored = StoredDelivery(
        DeliverySnapshot(
            purchase_id=f"buff:{ORDER}",
            buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=DeliveryStatus.AWAITING_OFFER,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        2,
    )
    host = [{"_db_id": 1, "buff_order_id": ORDER, "pending_receipt": True, "assetid": None}]
    permit = preflight_canary_permit(
        host_purchases=host,
        unresolved_checkout=None,
        recoverable_deliveries=(stored,),
        target_stored=stored,
        target_db_id=1,
        target_buff_order_id=ORDER,
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        expected_counterparty_steam_id=None,
        expected_is_our_offer=True,
        permit_id="permit-2",
        owner_nonce="nonce-2",
        created_at=2.0,
    )
    assert permit.expected_counterparty_steam_id is None
    with pytest.raises(HostAutoOfferIntegrationError):
        preflight_canary_permit(
            host_purchases=host,
            unresolved_checkout=None,
            recoverable_deliveries=(stored,),
            target_stored=stored,
            target_db_id=1,
            target_buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
            expected_counterparty_steam_id=None,
            expected_is_our_offer=False,
            permit_id="permit-3",
            owner_nonce="nonce-3",
            created_at=3.0,
        )
