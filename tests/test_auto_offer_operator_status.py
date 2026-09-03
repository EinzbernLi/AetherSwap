from __future__ import annotations

from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.operator_status import (
    AUTO_OFFER_DELIVERY_SCOPE_TEXT,
    build_delivery_attention_summary,
    format_operator_runtime_reason,
)
from app.auto_offer.store import StoredDelivery


def _stored(
    order_id: str,
    status: DeliveryStatus,
    *,
    mode: DeliveryMode = DeliveryMode.SELLER_SENDS_OFFER,
    pending_receipt: bool = True,
    assetid: str | None = None,
) -> StoredDelivery:
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id="account",
            recipient_steam_id="76561198000000001",
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=pending_receipt,
            assetid=assetid,
        ),
        1,
    )


def _purchase(order_id: str, *, pending_receipt: bool = True, assetid=None):
    return {
        "_db_id": 1,
        "buff_order_id": order_id,
        "pending_receipt": pending_receipt,
        "assetid": assetid,
    }


def test_principal_reason_uses_majority_of_existing_managed_delivery_states():
    summary = build_delivery_attention_summary(
        expected_active_delivery_count=3,
        purchases=[_purchase("one"), _purchase("two"), _purchase("three")],
        store_rows=[
            _stored("one", DeliveryStatus.AWAITING_OFFER),
            _stored("two", DeliveryStatus.AWAITING_OFFER),
            _stored("three", DeliveryStatus.PENDING_DIRECTION),
        ],
    )

    assert summary == {
        "principal_delivery_reason": "seller_offer",
        "principal_delivery_reason_count": 2,
    }


def test_equal_count_attention_state_wins_only_as_deterministic_tie_break():
    summary = build_delivery_attention_summary(
        expected_active_delivery_count=2,
        purchases=[_purchase("waiting"), _purchase("blocked")],
        store_rows=[
            _stored("waiting", DeliveryStatus.AWAITING_OFFER),
            _stored("blocked", DeliveryStatus.BLOCKED),
        ],
    )

    assert summary == {
        "principal_delivery_reason": "blocked_attention",
        "principal_delivery_reason_count": 1,
    }


def test_buyer_send_and_receipt_handoff_have_explicit_operator_reasons():
    buyer_wait = build_delivery_attention_summary(
        expected_active_delivery_count=1,
        purchases=[_purchase("buyer")],
        store_rows=[
            _stored(
                "buyer",
                DeliveryStatus.AWAITING_OFFER,
                mode=DeliveryMode.BUYER_SENDS_OFFER,
            )
        ],
    )
    receipt = build_delivery_attention_summary(
        expected_active_delivery_count=1,
        purchases=[_purchase("received")],
        store_rows=[
            _stored(
                "received",
                DeliveryStatus.RECEIVED,
                pending_receipt=False,
                assetid="asset-1",
            )
        ],
    )

    assert buyer_wait["principal_delivery_reason"] == "buyer_send_eligibility"
    assert receipt == {
        "principal_delivery_reason": "receipt_handoff",
        "principal_delivery_reason_count": 1,
    }


def test_refund_cleanup_orphan_matches_existing_runtime_active_count_semantics():
    summary = build_delivery_attention_summary(
        expected_active_delivery_count=1,
        purchases=[],
        store_rows=[
            _stored("cleanup", DeliveryStatus.REFUND_CLEANUP_PENDING),
        ],
    )

    assert summary == {
        "principal_delivery_reason": "refund_cleanup",
        "principal_delivery_reason_count": 1,
    }


def test_concurrent_or_contradictory_snapshot_never_invents_operator_truth():
    summary = build_delivery_attention_summary(
        expected_active_delivery_count=2,
        purchases=[_purchase("one")],
        store_rows=[_stored("one", DeliveryStatus.AWAITING_OFFER)],
    )

    assert summary == {
        "principal_delivery_reason": "summary_unavailable",
        "principal_delivery_reason_count": 0,
    }


def test_operator_reason_is_secret_free_scope_plus_wait_and_runtime_reason():
    text = format_operator_runtime_reason(
        "delivery_blocked",
        {
            "principal_delivery_reason": "blocked_attention",
            "principal_delivery_reason_count": 1,
        },
    )

    assert AUTO_OFFER_DELIVERY_SCOPE_TEXT in text
    assert "主要等待：需要人工处理阻止状态（1 单）" in text
    assert "运行原因：delivery_blocked" in text
    assert "cookie" not in text.lower()
    assert "steam_id" not in text.lower()
