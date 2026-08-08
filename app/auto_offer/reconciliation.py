"""Pure fail-closed planning for read-only Auto Offer evidence.

This module creates proposed snapshots only.  It does not own a store, perform
I/O, call a platform, or execute a transition.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .adapters import (
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformCapability,
    PlatformResult,
    PlatformResultStatus,
)
from .contracts import (
    AutoOfferResult,
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from .store import StoredDelivery


_IDENTITY_FIELDS = (
    "purchase_id",
    "buff_order_id",
    "account_id",
    "recipient_steam_id",
)


def _require_detail(value: object) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise DeliveryContractError("decision detail must be a non-whitespace string")


def _validate_delivery(delivery: object) -> None:
    if type(delivery) is not StoredDelivery:
        raise DeliveryContractError("delivery must be a StoredDelivery")
    if type(delivery.revision) is not int or delivery.revision < 1:
        raise DeliveryContractError("delivery revision must be an integer of at least one")
    validate_delivery_snapshot(delivery.snapshot)


def _validate_platform_result(platform_result: object) -> None:
    if type(platform_result) is not PlatformResult:
        raise DeliveryContractError("platform_result must be a PlatformResult")
    try:
        # Re-run the immutable result contract for defensive handling of a
        # forged instance created without invoking dataclass validation.
        PlatformResult.__post_init__(platform_result)
    except Exception as exc:
        raise DeliveryContractError("platform_result violates its contract") from exc


def _identity_matches(delivery: StoredDelivery, platform_result: PlatformResult) -> bool:
    request = platform_result.request
    snapshot = delivery.snapshot
    return all(getattr(request, field) == getattr(snapshot, field) for field in _IDENTITY_FIELDS) and (
        request.revision == delivery.revision
    )


def _decision(
    delivery: StoredDelivery,
    target: DeliverySnapshot | None,
    result: AutoOfferResult,
    retryable: bool,
    detail: str,
) -> ReconciliationDecision:
    return ReconciliationDecision(
        delivery=delivery,
        target=target,
        result=result,
        retryable=retryable,
        detail=detail,
    )


def _blocked(delivery: StoredDelivery, detail: str) -> ReconciliationDecision:
    return _decision(delivery, None, AutoOfferResult.BLOCKED, False, detail)


def _propose(
    delivery: StoredDelivery,
    target: DeliverySnapshot,
    detail: str,
) -> ReconciliationDecision:
    validate_delivery_snapshot(target)
    validate_delivery_transition(delivery.snapshot, target)
    return _decision(delivery, target, AutoOfferResult.WAITING, True, detail)


def _map_non_success(
    delivery: StoredDelivery,
    platform_result: PlatformResult,
) -> ReconciliationDecision:
    if platform_result.detail == "identity_mismatch":
        return _blocked(delivery, "identity_mismatch")
    status = platform_result.status
    if status is PlatformResultStatus.RESULT_UNKNOWN:
        return _decision(delivery, None, AutoOfferResult.WAITING, True, "read_result_unknown")
    if status is PlatformResultStatus.TIMEOUT:
        return _decision(delivery, None, AutoOfferResult.WAITING, True, "read_timeout")
    if status is PlatformResultStatus.FAILURE:
        return _blocked(delivery, "read_failure")
    if status is PlatformResultStatus.MALFORMED:
        return _blocked(delivery, "malformed_result")
    if status is PlatformResultStatus.UNSUPPORTED:
        return _blocked(delivery, "unsupported_capability")
    raise DeliveryContractError("unrecognized platform result status")


@dataclass(frozen=True)
class ReconciliationDecision:
    """An immutable proposed outcome; construction never persists it."""

    delivery: StoredDelivery
    target: DeliverySnapshot | None
    result: AutoOfferResult
    retryable: bool
    detail: str

    def __post_init__(self) -> None:
        _validate_delivery(self.delivery)
        if self.target is not None:
            validate_delivery_snapshot(self.target)
            validate_delivery_transition(self.delivery.snapshot, self.target)
            for field in _IDENTITY_FIELDS:
                if getattr(self.target, field) != getattr(self.delivery.snapshot, field):
                    raise DeliveryContractError("decision target identity cannot change")
        if type(self.result) is not AutoOfferResult:
            raise DeliveryContractError("decision result must be an AutoOfferResult")
        if type(self.retryable) is not bool:
            raise DeliveryContractError("decision retryable must be a bool")
        _require_detail(self.detail)


def plan_read_evidence_transition(
    delivery: StoredDelivery,
    platform_result: PlatformResult,
) -> ReconciliationDecision:
    """Plan one safe read-only transition without executing or persisting it."""

    _validate_delivery(delivery)
    _validate_platform_result(platform_result)

    # Identity is checked before interpreting status or evidence semantics.
    if not _identity_matches(delivery, platform_result):
        return _blocked(delivery, "identity_mismatch")

    if platform_result.status is not PlatformResultStatus.SUCCESS:
        return _map_non_success(delivery, platform_result)

    snapshot = delivery.snapshot
    request = platform_result.request
    evidence = platform_result.evidence

    if snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
        return _decision(
            delivery,
            None,
            AutoOfferResult.WAITING,
            True,
            "result_unknown_recovery_not_planned",
        )

    if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
        if (
            request.capability is PlatformCapability.READ_DELIVERY_DIRECTION
            and type(evidence) is DeliveryDirectionEvidence
            and evidence.direction == "seller_sends_offer"
        ):
            target = replace(
                snapshot,
                delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
                delivery_status=DeliveryStatus.AWAITING_OFFER,
            )
            return _propose(delivery, target, "seller_direction_proven")
        return _blocked(delivery, "evidence_not_allowed")

    if snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER:
        if snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER:
            return _blocked(delivery, "write_capability_required")
        if (
            snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
            and request.capability is PlatformCapability.READ_OFFER_STATE
            and type(evidence) is OfferStateEvidence
        ):
            target = replace(
                snapshot,
                delivery_status=DeliveryStatus.OFFER_RECEIVED,
                steam_tradeoffer_id=evidence.steam_tradeoffer_id,
            )
            return _propose(delivery, target, "seller_offer_proven")
        return _blocked(delivery, "evidence_not_allowed")

    if snapshot.delivery_status is DeliveryStatus.AWAITING_INVENTORY:
        if (
            request.capability is PlatformCapability.READ_INVENTORY_STATE
            and type(evidence) is InventoryStateEvidence
        ):
            return _decision(
                delivery,
                None,
                AutoOfferResult.WAITING,
                True,
                "purchase_asset_not_proven",
            )
        return _blocked(delivery, "evidence_not_allowed")

    if snapshot.delivery_status is DeliveryStatus.RECEIVED:
        return _decision(delivery, None, AutoOfferResult.COMPLETE, False, "already_received")
    if snapshot.delivery_status in {
        DeliveryStatus.BLOCKED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }:
        return _blocked(delivery, "terminal_delivery_state")

    return _blocked(delivery, "transition_not_plannable")


__all__ = ["ReconciliationDecision", "plan_read_evidence_transition"]
