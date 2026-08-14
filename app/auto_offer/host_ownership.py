"""Fail-closed ownership classification for Host Purchase mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .contracts import DeliveryStatus
from .store import AutoOfferStore, AutoOfferStoreError, StoredDelivery


AUTO_OFFER_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"
_FROZEN_DELIVERY_FIELDS = frozenset(
    {"buff_order_id", "goods_id", "pending_receipt", "assetid"}
)


class HostPurchaseOwnership(str, Enum):
    UNOWNED = "unowned"
    MANAGED = "managed"
    RECEIPT_PENDING = "receipt_pending"
    RELEASED = "released"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class HostPurchaseOwnershipDecision:
    ownership: HostPurchaseOwnership
    stored: StoredDelivery | None
    reason: str


class HostPurchaseMutationBlockedError(RuntimeError):
    """A generic Host Purchase mutation would violate Auto Offer ownership."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _exact_buff_order_id(value: object) -> str | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return value


def _empty_assetid(value: object) -> bool:
    return value is None or value == ""


def _store_index(
    store_path: str | Path | None = None,
) -> dict[str, StoredDelivery]:
    path = AUTO_OFFER_STORE_PATH if store_path is None else Path(store_path)
    try:
        stored = AutoOfferStore.inspect_existing(path)
    except AutoOfferStoreError as exc:
        raise HostPurchaseMutationBlockedError(
            "AUTO_OFFER_OWNERSHIP_UNSAFE"
        ) from exc
    result: dict[str, StoredDelivery] = {}
    for item in stored:
        order_id = item.snapshot.buff_order_id
        if order_id in result:
            raise HostPurchaseMutationBlockedError("AUTO_OFFER_OWNERSHIP_UNSAFE")
        result[order_id] = item
    return result


def classify_host_purchase(
    purchase: Mapping[str, object],
    *,
    store_index: Mapping[str, StoredDelivery] | None = None,
    store_path: str | Path | None = None,
) -> HostPurchaseOwnershipDecision:
    if not isinstance(purchase, Mapping):
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNSAFE,
            None,
            "host_purchase_invalid",
        )

    order_id = _exact_buff_order_id(purchase.get("buff_order_id"))
    if order_id is None:
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNOWNED,
            None,
            "no_exact_buff_order_id",
        )

    if store_index is None:
        try:
            stored = AutoOfferStore.inspect_existing_by_buff_order_id(
                AUTO_OFFER_STORE_PATH if store_path is None else Path(store_path),
                order_id,
            )
        except AutoOfferStoreError:
            return HostPurchaseOwnershipDecision(
                HostPurchaseOwnership.UNSAFE,
                None,
                "store_unreadable",
            )
    else:
        stored = store_index.get(order_id)

    if stored is None:
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNOWNED,
            None,
            "no_store_row",
        )

    snapshot = stored.snapshot
    if snapshot.buff_order_id != order_id or snapshot.purchase_id != f"buff:{order_id}":
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNSAFE,
            stored,
            "delivery_identity_mismatch",
        )

    host_pending = purchase.get("pending_receipt")
    host_asset = purchase.get("assetid")

    if snapshot.delivery_status in {
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }:
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNSAFE,
            stored,
            "terminal_tombstone_has_host_purchase",
        )

    if snapshot.delivery_status is DeliveryStatus.RECEIVED:
        store_asset = snapshot.assetid
        if (
            snapshot.pending_receipt
            or type(store_asset) is not str
            or not store_asset
            or store_asset.strip() != store_asset
        ):
            return HostPurchaseOwnershipDecision(
                HostPurchaseOwnership.UNSAFE,
                stored,
                "received_store_invalid",
            )
        if host_pending is True and _empty_assetid(host_asset):
            return HostPurchaseOwnershipDecision(
                HostPurchaseOwnership.RECEIPT_PENDING,
                stored,
                "exact_receipt_handoff_pending",
            )
        if (
            host_pending is False
            and type(host_asset) is str
            and host_asset == store_asset
        ):
            return HostPurchaseOwnershipDecision(
                HostPurchaseOwnership.RELEASED,
                stored,
                "exact_receipt_released",
            )
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.UNSAFE,
            stored,
            "receipt_identity_mismatch",
        )

    if (
        snapshot.pending_receipt is True
        and host_pending is True
        and _empty_assetid(snapshot.assetid)
        and _empty_assetid(host_asset)
    ):
        return HostPurchaseOwnershipDecision(
            HostPurchaseOwnership.MANAGED,
            stored,
            "auto_offer_delivery_managed",
        )

    return HostPurchaseOwnershipDecision(
        HostPurchaseOwnership.UNSAFE,
        stored,
        "managed_host_state_mismatch",
    )


def require_purchase_mutation_allowed(
    purchase: Mapping[str, object],
    *,
    operation: str,
    data: Mapping[str, object] | None = None,
    store_index: Mapping[str, StoredDelivery] | None = None,
    store_path: str | Path | None = None,
) -> HostPurchaseOwnershipDecision:
    decision = classify_host_purchase(
        purchase,
        store_index=store_index,
        store_path=store_path,
    )
    if decision.ownership in {
        HostPurchaseOwnership.MANAGED,
        HostPurchaseOwnership.RECEIPT_PENDING,
    }:
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_PURCHASE_MANAGED")
    if decision.ownership is HostPurchaseOwnership.UNSAFE:
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_OWNERSHIP_UNSAFE")
    if (
        decision.ownership is HostPurchaseOwnership.RELEASED
        and operation == "update"
        and data is not None
        and _FROZEN_DELIVERY_FIELDS.intersection(data)
    ):
        raise HostPurchaseMutationBlockedError(
            "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE"
        )
    return decision


def require_broad_transaction_mutation_allowed(
    current_purchases: Sequence[Mapping[str, object]],
    *,
    proposed_purchases: Sequence[Mapping[str, object]] | None = None,
    store_path: str | Path | None = None,
) -> None:
    try:
        index = _store_index(store_path)
    except HostPurchaseMutationBlockedError:
        raise

    released_current: dict[str, Mapping[str, object]] = {}
    for purchase in current_purchases:
        decision = require_purchase_mutation_allowed(
            purchase,
            operation="broad",
            store_index=index,
        )
        if decision.ownership is HostPurchaseOwnership.RELEASED:
            order_id = decision.stored.snapshot.buff_order_id
            if order_id in released_current:
                raise HostPurchaseMutationBlockedError(
                    "AUTO_OFFER_OWNERSHIP_UNSAFE"
                )
            released_current[order_id] = purchase

    if proposed_purchases is None:
        return

    released_proposed: dict[str, Mapping[str, object]] = {}
    for purchase in proposed_purchases:
        decision = require_purchase_mutation_allowed(
            purchase,
            operation="broad",
            store_index=index,
        )
        if decision.ownership is HostPurchaseOwnership.RELEASED:
            order_id = decision.stored.snapshot.buff_order_id
            if order_id in released_proposed:
                raise HostPurchaseMutationBlockedError(
                    "AUTO_OFFER_OWNERSHIP_UNSAFE"
                )
            released_proposed[order_id] = purchase

    for order_id, current in released_current.items():
        proposed = released_proposed.get(order_id)
        if proposed is None:
            continue
        for field in _FROZEN_DELIVERY_FIELDS:
            if current.get(field) != proposed.get(field):
                raise HostPurchaseMutationBlockedError(
                    "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE"
                )


__all__ = [
    "AUTO_OFFER_STORE_PATH",
    "HostPurchaseMutationBlockedError",
    "HostPurchaseOwnership",
    "HostPurchaseOwnershipDecision",
    "classify_host_purchase",
    "require_broad_transaction_mutation_allowed",
    "require_purchase_mutation_allowed",
]
