"""Secret-free operator summary for the existing Auto Offer delivery lifecycle.

This module is presentation-only. It derives one principal wait/attention reason
from the authoritative Host Purchase + Auto Offer Store snapshot. It creates no
state, performs no platform action, and never changes delivery authority.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .contracts import DeliveryMode, DeliveryStatus
from .host_ownership import (
    AUTO_OFFER_STORE_PATH,
    HostPurchaseOwnership,
    classify_host_purchase,
)
from .store import AutoOfferStore, StoredDelivery


AUTO_OFFER_DELIVERY_SCOPE_TEXT = (
    "Auto Offer 仅负责交付/收货；购买、支付、库存、上架和售出仍由 Host 处理"
)

_DELIVERY_REASON_LABELS = {
    "delivery_direction": "等待确认交付方向",
    "seller_offer": "等待卖家发送报价",
    "buyer_send_eligibility": "等待买家报价发送条件",
    "offer_identity": "等待报价身份/状态确认",
    "offer_completion": "等待 Steam 报价推进",
    "mobile_confirmation": "等待移动确认",
    "confirmation_result": "等待确认结果",
    "seller_offer_acceptance": "等待接受卖家报价",
    "accept_result": "等待接受结果",
    "inventory_receipt": "等待库存到账",
    "refund_terminal_evidence": "等待退款终态证明",
    "refund_cleanup": "等待退款清理",
    "receipt_handoff": "等待 Host 收货落账",
    "result_unknown_attention": "需要人工核对写入结果",
    "blocked_attention": "需要人工处理阻止状态",
    "summary_unavailable": "在途状态汇总不可用",
}

# Count is authoritative for "principal"; this order only breaks equal-count
# ties so operator-attention states win over ordinary waits deterministically.
_DELIVERY_REASON_PRIORITY = {
    code: index
    for index, code in enumerate(
        (
            "blocked_attention",
            "result_unknown_attention",
            "refund_cleanup",
            "refund_terminal_evidence",
            "receipt_handoff",
            "inventory_receipt",
            "mobile_confirmation",
            "confirmation_result",
            "seller_offer_acceptance",
            "accept_result",
            "offer_completion",
            "offer_identity",
            "seller_offer",
            "buyer_send_eligibility",
            "delivery_direction",
        )
    )
}


def _unavailable_summary() -> dict[str, object]:
    return {
        "principal_delivery_reason": "summary_unavailable",
        "principal_delivery_reason_count": 0,
    }


def _reason_code_for_managed(stored: StoredDelivery) -> str | None:
    snapshot = stored.snapshot
    status = snapshot.delivery_status
    mode = snapshot.delivery_mode

    if status is DeliveryStatus.PENDING_DIRECTION:
        return "delivery_direction"
    if status is DeliveryStatus.AWAITING_OFFER:
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return "seller_offer"
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            return "buyer_send_eligibility"
        return None
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        return "offer_identity"
    if status in {DeliveryStatus.OFFER_SENT, DeliveryStatus.OFFER_CONFIRMED}:
        return "offer_completion"
    if status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED:
        return "mobile_confirmation"
    if status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED:
        return "confirmation_result"
    if status is DeliveryStatus.OFFER_RECEIVED:
        return "seller_offer_acceptance"
    if status is DeliveryStatus.OFFER_ACCEPT_ATTEMPTED:
        return "accept_result"
    if status is DeliveryStatus.AWAITING_INVENTORY:
        return "inventory_receipt"
    if status is DeliveryStatus.OFFER_TERMINATED:
        return "refund_terminal_evidence"
    if status is DeliveryStatus.REFUND_CLEANUP_PENDING:
        return "refund_cleanup"
    if status is DeliveryStatus.RESULT_UNKNOWN:
        return "result_unknown_attention"
    if status is DeliveryStatus.BLOCKED:
        return "blocked_attention"
    return None


def build_delivery_attention_summary(
    *,
    expected_active_delivery_count: int,
    purchases: Sequence[Mapping[str, object]] | None = None,
    store_rows: Sequence[StoredDelivery] | None = None,
    store_path: str | Path | None = None,
) -> dict[str, object]:
    """Return one deterministic principal wait/attention code and its count.

    The summary deliberately fails closed to ``summary_unavailable`` whenever
    the independently read presentation snapshot cannot be reconciled with the
    already-computed active-delivery count. That avoids presenting stale or
    contradictory operator truth during a concurrent transition.
    """

    if (
        type(expected_active_delivery_count) is not int
        or expected_active_delivery_count < 0
    ):
        return _unavailable_summary()
    if expected_active_delivery_count == 0:
        return {
            "principal_delivery_reason": None,
            "principal_delivery_reason_count": 0,
        }

    try:
        rows = list(
            AutoOfferStore.inspect_existing(
                AUTO_OFFER_STORE_PATH if store_path is None else Path(store_path)
            )
            if store_rows is None
            else store_rows
        )
        index: dict[str, StoredDelivery] = {}
        for stored in rows:
            if type(stored) is not StoredDelivery:
                return _unavailable_summary()
            order_id = stored.snapshot.buff_order_id
            if (
                type(order_id) is not str
                or not order_id
                or order_id.strip() != order_id
                or order_id in index
            ):
                return _unavailable_summary()
            index[order_id] = stored

        if purchases is None:
            from app.state import get_purchases

            purchases = get_purchases()
        if not isinstance(purchases, Sequence) or isinstance(
            purchases, (str, bytes)
        ):
            return _unavailable_summary()

        counts: Counter[str] = Counter()
        host_order_ids: set[str] = set()
        for purchase in purchases:
            if not isinstance(purchase, Mapping):
                return _unavailable_summary()
            order_id = purchase.get("buff_order_id")
            if isinstance(order_id, str) and order_id:
                if order_id in host_order_ids:
                    return _unavailable_summary()
                host_order_ids.add(order_id)

            decision = classify_host_purchase(purchase, store_index=index)
            if decision.ownership is HostPurchaseOwnership.RECEIPT_PENDING:
                counts["receipt_handoff"] += 1
                continue
            if decision.ownership is HostPurchaseOwnership.MANAGED:
                if decision.stored is None:
                    return _unavailable_summary()
                code = _reason_code_for_managed(decision.stored)
                if code is None:
                    return _unavailable_summary()
                counts[code] += 1
                continue
            if decision.ownership is HostPurchaseOwnership.UNSAFE:
                return _unavailable_summary()

        # Crash-safe refund cleanup may intentionally outlive the Host row; the
        # runtime lifecycle counts this exact orphan state as still active.
        for order_id, stored in index.items():
            if (
                order_id not in host_order_ids
                and stored.snapshot.delivery_status
                is DeliveryStatus.REFUND_CLEANUP_PENDING
            ):
                counts["refund_cleanup"] += 1

        if sum(counts.values()) != expected_active_delivery_count or not counts:
            return _unavailable_summary()

        principal, count = min(
            counts.items(),
            key=lambda item: (
                -item[1],
                _DELIVERY_REASON_PRIORITY.get(item[0], len(_DELIVERY_REASON_PRIORITY)),
                item[0],
            ),
        )
        return {
            "principal_delivery_reason": principal,
            "principal_delivery_reason_count": count,
        }
    except Exception:
        return _unavailable_summary()


def format_operator_runtime_reason(
    runtime_reason_code: str | None,
    summary: Mapping[str, object],
) -> str:
    """Render the fixed responsibility boundary plus current wait truth."""

    parts = [f"职责范围：{AUTO_OFFER_DELIVERY_SCOPE_TEXT}"]
    principal = summary.get("principal_delivery_reason")
    count = summary.get("principal_delivery_reason_count")
    if isinstance(principal, str):
        label = _DELIVERY_REASON_LABELS.get(
            principal,
            _DELIVERY_REASON_LABELS["summary_unavailable"],
        )
        suffix = f"（{count} 单）" if type(count) is int and count > 0 else ""
        parts.append(f"主要等待：{label}{suffix}")
    if runtime_reason_code:
        parts.append(f"运行原因：{runtime_reason_code}")
    return "；".join(parts)


__all__ = [
    "AUTO_OFFER_DELIVERY_SCOPE_TEXT",
    "build_delivery_attention_summary",
    "format_operator_runtime_reason",
]
