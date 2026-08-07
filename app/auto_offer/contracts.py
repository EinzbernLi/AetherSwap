"""Pure, fail-closed contracts for the native Auto Offer module.

No database, network, BUFF, or Steam code belongs in this module.  It defines
the values and invariants that a future native runtime may use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final


class DeliveryContractError(ValueError):
    """Raised when a result, snapshot, or state transition is unsafe."""


class AutoOfferResult(str, Enum):
    DISABLED = "disabled"
    COMPLETE = "complete"
    WAITING = "waiting"
    RESULT_UNKNOWN = "result_unknown"
    BLOCKED = "blocked"


class DeliveryMode(str, Enum):
    SELLER_SENDS_OFFER = "seller_sends_offer"
    BUYER_SENDS_OFFER = "buyer_sends_offer"


class DeliveryStatus(str, Enum):
    PENDING_DIRECTION = "pending_direction"
    AWAITING_OFFER = "awaiting_offer"
    OFFER_ATTEMPTED = "offer_attempted"
    OFFER_SENT = "offer_sent"
    OFFER_RECEIVED = "offer_received"
    OFFER_CONFIRMED = "offer_confirmed"
    AWAITING_INVENTORY = "awaiting_inventory"
    RECEIVED = "received"
    RESULT_UNKNOWN = "result_unknown"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


DELIVERY_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "steam_id_mismatch",
        "contract_unknown",
        "write_result_unknown",
        "offer_not_found",
        "inventory_reconciliation_failed",
        "module_disabled",
        "module_contract_mismatch",
    }
)

TERMINAL_DELIVERY_STATUSES: Final[frozenset[DeliveryStatus]] = frozenset(
    {
        DeliveryStatus.RECEIVED,
        DeliveryStatus.BLOCKED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }
)

NORMAL_DIRECTION_REQUIRED_STATUSES: Final[frozenset[DeliveryStatus]] = frozenset(
    {
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    }
)


@dataclass(frozen=True)
class DeliverySnapshot:
    """Immutable delivery state supplied to contract validation."""

    purchase_id: str
    buff_order_id: str
    account_id: str
    recipient_steam_id: str
    delivery_mode: DeliveryMode | None
    delivery_status: DeliveryStatus
    steam_tradeoffer_id: str | None
    offer_attempted_at: float | None
    offer_sent_at: float | None
    received_at: float | None
    delivery_error: str | None
    pending_receipt: bool
    assetid: str | None


def _require_enum(value: object, enum_type: type[Enum], field: str) -> None:
    if type(value) is not enum_type:
        raise DeliveryContractError(f"{field} must be a {enum_type.__name__}")


def _require_id(value: object, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str:
        raise DeliveryContractError(f"{field} must be a string")
    if not value or value.strip() != value:
        raise DeliveryContractError(f"{field} must be a non-whitespace ID")


def _require_timestamp(value: object, field: str) -> None:
    if value is None:
        return
    if type(value) not in (int, float):
        raise DeliveryContractError(f"{field} must be a finite non-negative number")
    if not math.isfinite(value) or value < 0:
        raise DeliveryContractError(f"{field} must be a finite non-negative number")


def _validate_snapshot_shape(snapshot: DeliverySnapshot) -> None:
    if type(snapshot) is not DeliverySnapshot:
        raise DeliveryContractError("snapshot must be a DeliverySnapshot")

    for field in ("purchase_id", "buff_order_id", "account_id", "recipient_steam_id"):
        _require_id(getattr(snapshot, field), field)
    _require_id(snapshot.steam_tradeoffer_id, "steam_tradeoffer_id", optional=True)
    _require_id(snapshot.assetid, "assetid", optional=True)

    if type(snapshot.pending_receipt) is not bool:
        raise DeliveryContractError("pending_receipt must be a bool")

    for field in ("offer_attempted_at", "offer_sent_at", "received_at"):
        _require_timestamp(getattr(snapshot, field), field)

    if snapshot.delivery_mode is not None:
        _require_enum(snapshot.delivery_mode, DeliveryMode, "delivery_mode")
    _require_enum(snapshot.delivery_status, DeliveryStatus, "delivery_status")

    if snapshot.delivery_error is not None:
        if type(snapshot.delivery_error) is not str or snapshot.delivery_error not in DELIVERY_ERROR_CODES:
            raise DeliveryContractError("delivery_error is not an allowed code")


def validate_delivery_snapshot(snapshot: DeliverySnapshot) -> None:
    """Validate IDs, timestamps, direction, receipt proof, and error codes."""
    _validate_snapshot_shape(snapshot)
    status = snapshot.delivery_status
    mode = snapshot.delivery_mode

    if status is DeliveryStatus.PENDING_DIRECTION:
        if mode is not None:
            raise DeliveryContractError("pending_direction requires an unknown mode")
    elif status in NORMAL_DIRECTION_REQUIRED_STATUSES and mode is None:
        raise DeliveryContractError(f"{status.value} requires a delivery mode")

    if status is DeliveryStatus.OFFER_ATTEMPTED:
        if mode is not DeliveryMode.BUYER_SENDS_OFFER:
            raise DeliveryContractError("offer_attempted requires buyer mode")
        if snapshot.offer_attempted_at is None:
            raise DeliveryContractError("offer_attempted requires offer_attempted_at")
        if snapshot.steam_tradeoffer_id is not None:
            raise DeliveryContractError("offer_attempted cannot have a trade offer ID")
        if snapshot.offer_sent_at is not None:
            raise DeliveryContractError("offer_attempted cannot have offer_sent_at")
        if snapshot.received_at is not None:
            raise DeliveryContractError("offer_attempted cannot have received_at")
    elif status is DeliveryStatus.OFFER_SENT:
        if mode is not DeliveryMode.BUYER_SENDS_OFFER:
            raise DeliveryContractError("offer_sent requires buyer mode")
        if snapshot.offer_attempted_at is None:
            raise DeliveryContractError("offer_sent requires offer_attempted_at")
        if snapshot.offer_sent_at is None:
            raise DeliveryContractError("offer_sent requires offer_sent_at")
        if snapshot.steam_tradeoffer_id is None:
            raise DeliveryContractError("offer_sent requires a trade offer ID")
        if snapshot.received_at is not None:
            raise DeliveryContractError("offer_sent cannot have received_at")
    elif status is DeliveryStatus.OFFER_RECEIVED:
        if mode is not DeliveryMode.SELLER_SENDS_OFFER:
            raise DeliveryContractError("offer_received requires seller mode")
        if snapshot.steam_tradeoffer_id is None:
            raise DeliveryContractError("offer_received requires a trade offer ID")
        if snapshot.offer_attempted_at is not None or snapshot.offer_sent_at is not None:
            raise DeliveryContractError("seller offer_received cannot have buyer timing")
        if snapshot.received_at is not None:
            raise DeliveryContractError("offer_received cannot have received_at")

    if status is DeliveryStatus.OFFER_CONFIRMED:
        if snapshot.steam_tradeoffer_id is None:
            raise DeliveryContractError("offer_confirmed requires a trade offer ID")
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.offer_attempted_at is None or snapshot.offer_sent_at is None:
                raise DeliveryContractError("buyer offer_confirmed requires buyer timing")
        elif mode is DeliveryMode.SELLER_SENDS_OFFER:
            if snapshot.offer_attempted_at is not None or snapshot.offer_sent_at is not None:
                raise DeliveryContractError("seller offer_confirmed cannot have buyer timing")
        if snapshot.received_at is not None:
            raise DeliveryContractError("offer_confirmed cannot have received_at")

    if status is DeliveryStatus.AWAITING_INVENTORY:
        if snapshot.steam_tradeoffer_id is None:
            raise DeliveryContractError("awaiting_inventory requires a trade offer ID")
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.offer_attempted_at is None or snapshot.offer_sent_at is None:
                raise DeliveryContractError("buyer awaiting_inventory requires buyer timing")
        elif mode is DeliveryMode.SELLER_SENDS_OFFER:
            if snapshot.offer_attempted_at is not None or snapshot.offer_sent_at is not None:
                raise DeliveryContractError("seller awaiting_inventory cannot have buyer timing")
        if snapshot.received_at is not None:
            raise DeliveryContractError("awaiting_inventory cannot have received_at")

    if status is DeliveryStatus.RECEIVED:
        if snapshot.pending_receipt:
            raise DeliveryContractError("received cannot have a pending receipt")
        for field in ("steam_tradeoffer_id", "assetid", "received_at"):
            if getattr(snapshot, field) is None:
                raise DeliveryContractError(f"received requires {field}")
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.offer_attempted_at is None or snapshot.offer_sent_at is None:
                raise DeliveryContractError("buyer received requires buyer timing")
        elif mode is DeliveryMode.SELLER_SENDS_OFFER:
            if snapshot.offer_attempted_at is not None or snapshot.offer_sent_at is not None:
                raise DeliveryContractError("seller received cannot have buyer timing")
    elif not snapshot.pending_receipt:
        raise DeliveryContractError("non-received status requires a pending receipt")

    if status is DeliveryStatus.RESULT_UNKNOWN:
        if snapshot.delivery_error != "write_result_unknown":
            raise DeliveryContractError("result_unknown requires write_result_unknown")
        if snapshot.received_at is not None:
            raise DeliveryContractError("result_unknown cannot have received_at")

    if (
        snapshot.offer_attempted_at is not None
        and snapshot.offer_sent_at is not None
        and snapshot.offer_attempted_at > snapshot.offer_sent_at
    ):
        raise DeliveryContractError("offer_attempted_at must not be after offer_sent_at")
    if (
        snapshot.offer_sent_at is not None
        and snapshot.received_at is not None
        and snapshot.offer_sent_at > snapshot.received_at
    ):
        raise DeliveryContractError("offer_sent_at must not be after received_at")
    if (
        snapshot.offer_attempted_at is not None
        and snapshot.received_at is not None
        and snapshot.offer_attempted_at > snapshot.received_at
    ):
        raise DeliveryContractError("offer_attempted_at must not be after received_at")


_BUYER_PATH: Final[tuple[DeliveryStatus, ...]] = (
    DeliveryStatus.PENDING_DIRECTION,
    DeliveryStatus.AWAITING_OFFER,
    DeliveryStatus.OFFER_ATTEMPTED,
    DeliveryStatus.OFFER_SENT,
    DeliveryStatus.OFFER_CONFIRMED,
    DeliveryStatus.AWAITING_INVENTORY,
    DeliveryStatus.RECEIVED,
)
_SELLER_PATH: Final[tuple[DeliveryStatus, ...]] = (
    DeliveryStatus.PENDING_DIRECTION,
    DeliveryStatus.AWAITING_OFFER,
    DeliveryStatus.OFFER_RECEIVED,
    DeliveryStatus.OFFER_CONFIRMED,
    DeliveryStatus.AWAITING_INVENTORY,
    DeliveryStatus.RECEIVED,
)


def _transition_statuses(
    current: DeliveryStatus,
    target: DeliveryStatus,
    mode: DeliveryMode | None,
) -> None:
    if mode is not None:
        _require_enum(mode, DeliveryMode, "delivery_mode")
    if current in TERMINAL_DELIVERY_STATUSES:
        raise DeliveryContractError("terminal delivery status cannot transition")
    if current is target:
        raise DeliveryContractError("delivery transition must change status")

    if target is DeliveryStatus.RESULT_UNKNOWN:
        return
    if target in {
        DeliveryStatus.BLOCKED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }:
        return
    if current is DeliveryStatus.RESULT_UNKNOWN and target is DeliveryStatus.OFFER_ATTEMPTED:
        raise DeliveryContractError("result_unknown cannot transition to offer_attempted")

    if current is DeliveryStatus.RESULT_UNKNOWN:
        if mode is None:
            raise DeliveryContractError("result_unknown recovery requires a delivery mode")
        _require_enum(mode, DeliveryMode, "delivery_mode")
        path = _BUYER_PATH if mode is DeliveryMode.BUYER_SENDS_OFFER else _SELLER_PATH
        if target not in path or target in {
            DeliveryStatus.PENDING_DIRECTION,
            DeliveryStatus.AWAITING_OFFER,
            DeliveryStatus.OFFER_ATTEMPTED,
        }:
            raise DeliveryContractError("result_unknown recovery requires later evidence")
        return

    if mode is None:
        raise DeliveryContractError("this transition requires a delivery mode")
    path = _BUYER_PATH if mode is DeliveryMode.BUYER_SENDS_OFFER else _SELLER_PATH
    try:
        current_index = path.index(current)
        target_index = path.index(target)
    except ValueError as exc:
        raise DeliveryContractError("status is not valid for the delivery mode") from exc
    if target_index != current_index + 1:
        raise DeliveryContractError("delivery status transition is not allowed")


def validate_delivery_transition(
    current: DeliveryStatus | DeliverySnapshot,
    target: DeliveryStatus | DeliverySnapshot,
    delivery_mode: DeliveryMode | None = None,
) -> None:
    """Validate one forward transition without any resend/override escape hatch.

    Status values may be supplied directly with a mode, or as snapshots.  The
    snapshot form validates both states before applying the transition rules.
    """
    if isinstance(current, DeliverySnapshot) or isinstance(target, DeliverySnapshot):
        if type(current) is not DeliverySnapshot or type(target) is not DeliverySnapshot:
            raise DeliveryContractError("transition endpoints must use the same form")
        validate_delivery_snapshot(current)
        validate_delivery_snapshot(target)
        if (
            current.purchase_id,
            current.buff_order_id,
            current.account_id,
            current.recipient_steam_id,
        ) != (
            target.purchase_id,
            target.buff_order_id,
            target.account_id,
            target.recipient_steam_id,
        ):
            raise DeliveryContractError("transition endpoints must identify one delivery")
        if current.delivery_mode is not None and target.delivery_mode != current.delivery_mode:
            raise DeliveryContractError("delivery mode cannot change")
        if delivery_mode is not None:
            raise DeliveryContractError("snapshot transitions must derive delivery mode")
        delivery_mode = target.delivery_mode or current.delivery_mode
        current = current.delivery_status
        target = target.delivery_status

    _require_enum(current, DeliveryStatus, "current")
    _require_enum(target, DeliveryStatus, "target")
    _transition_statuses(current, target, delivery_mode)


def result_blocks_next_purchase(result: AutoOfferResult) -> bool:
    """Return whether an Auto Offer result must stop the next purchase."""
    if type(result) is not AutoOfferResult:
        raise DeliveryContractError("result must be an AutoOfferResult")
    return result in {
        AutoOfferResult.WAITING,
        AutoOfferResult.RESULT_UNKNOWN,
        AutoOfferResult.BLOCKED,
    }


__all__ = [
    "AutoOfferResult",
    "DELIVERY_ERROR_CODES",
    "DeliveryContractError",
    "DeliveryMode",
    "DeliverySnapshot",
    "DeliveryStatus",
    "NORMAL_DIRECTION_REQUIRED_STATUSES",
    "TERMINAL_DELIVERY_STATUSES",
    "result_blocks_next_purchase",
    "validate_delivery_snapshot",
    "validate_delivery_transition",
]
