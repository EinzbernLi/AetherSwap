"""One-shot read-only platform step coordination.

The coordinator owns routing, adapter normalization, and one optional Store
CAS write.  Reconciliation remains the only authority that can propose a
delivery target.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .adapters import (
    PlatformAdapter,
    PlatformAdapterError,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformAdapterUnsupportedError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from .contracts import (
    DeliveryContractError,
    DeliveryMode,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from .reconciliation import (
    ReconciliationDecision,
    plan_read_evidence_transition,
)
from .store import (
    AutoOfferStoreConflictError,
    AutoOfferStoreError,
    AutoOfferStoreStaleWriteError,
    StoredDelivery,
)


_READ_CAPABILITIES = frozenset(
    {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_INVENTORY_STATE,
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
    }
)
_TRADEOFFER_BOUND_CAPABILITIES = frozenset(
    {
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
    }
)
_IDENTITY_FIELDS = (
    "purchase_id",
    "buff_order_id",
    "account_id",
    "recipient_steam_id",
)


class ReadOnlyCoordinatorError(RuntimeError):
    """Base error for coordinator configuration and Store failures."""


class ReadOnlyCoordinatorConflictError(ReadOnlyCoordinatorError):
    """The supplied or persisted delivery changed across the step boundary."""


class ReadOnlyCoordinatorBlockedError(ReadOnlyCoordinatorError):
    """The current state or capability cannot be handled by this read-only step."""


def _validate_timeout(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ReadOnlyCoordinatorError("invalid_timeout")


def _validate_delivery(delivery: object) -> None:
    if type(delivery) is not StoredDelivery:
        raise ReadOnlyCoordinatorError("invalid_delivery")
    if type(delivery.revision) is not int or delivery.revision < 1:
        raise ReadOnlyCoordinatorError("invalid_delivery")
    try:
        validate_delivery_snapshot(delivery.snapshot)
    except DeliveryContractError as exc:
        raise ReadOnlyCoordinatorError("invalid_delivery") from exc


def _request_matches_delivery(
    request: PlatformRequest,
    delivery: StoredDelivery,
) -> bool:
    snapshot = delivery.snapshot
    identity_matches = all(
        getattr(request, field) == getattr(snapshot, field)
        for field in _IDENTITY_FIELDS
    ) and request.revision == delivery.revision
    expected_tradeoffer_id = (
        snapshot.steam_tradeoffer_id
        if request.capability in _TRADEOFFER_BOUND_CAPABILITIES
        else None
    )
    return identity_matches and request.steam_tradeoffer_id == expected_tradeoffer_id


def _request_matches(
    left: PlatformRequest,
    right: PlatformRequest,
) -> bool:
    return (
        left.purchase_id == right.purchase_id
        and left.buff_order_id == right.buff_order_id
        and left.account_id == right.account_id
        and left.recipient_steam_id == right.recipient_steam_id
        and left.revision == right.revision
        and left.capability is right.capability
        and left.timeout_seconds == right.timeout_seconds
        and left.steam_tradeoffer_id == right.steam_tradeoffer_id
    )


def _normalize_result(
    request: PlatformRequest,
    raw_result: object,
) -> PlatformResult:
    if type(raw_result) is not PlatformResult:
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.MALFORMED,
            detail="adapter_result_invalid",
        )
    try:
        PlatformResult.__post_init__(raw_result)
        if not _request_matches(raw_result.request, request):
            raise ValueError("request mismatch")
    except Exception:
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.MALFORMED,
            detail="adapter_result_invalid",
        )
    return raw_result


def _exception_result(request: PlatformRequest, error: BaseException) -> PlatformResult:
    if isinstance(error, PlatformAdapterTimeoutError):
        status = PlatformResultStatus.TIMEOUT
        detail = "adapter_timeout"
    elif isinstance(error, PlatformAdapterUnsupportedError):
        status = PlatformResultStatus.UNSUPPORTED
        detail = "adapter_unsupported"
    elif isinstance(error, PlatformAdapterProtocolError):
        status = PlatformResultStatus.MALFORMED
        detail = "adapter_protocol_error"
    elif isinstance(error, PlatformAdapterError):
        status = PlatformResultStatus.FAILURE
        detail = "adapter_failure"
    else:
        status = PlatformResultStatus.FAILURE
        detail = "adapter_internal_error"
    return PlatformResult(request=request, status=status, detail=detail)


def _required_capability(delivery: StoredDelivery) -> PlatformCapability:
    status = delivery.snapshot.delivery_status
    mode = delivery.snapshot.delivery_mode
    if status is DeliveryStatus.PENDING_DIRECTION:
        return PlatformCapability.READ_DELIVERY_DIRECTION
    if status is DeliveryStatus.AWAITING_OFFER:
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            raise ReadOnlyCoordinatorBlockedError("write_capability_required")
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return PlatformCapability.READ_OFFER_STATE
    if status is DeliveryStatus.AWAITING_INVENTORY:
        return PlatformCapability.READ_STEAM_COMPLETED_TRADE
    if status is DeliveryStatus.OFFER_RECEIVED:
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status is DeliveryStatus.OFFER_SENT:
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status is DeliveryStatus.OFFER_CONFIRMED:
        if mode in {
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryMode.BUYER_SENDS_OFFER,
        }:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    raise ReadOnlyCoordinatorBlockedError("read_step_not_available")


def _validate_store(store: object) -> None:
    try:
        get_current = getattr(store, "get_by_purchase_id")
        advance = getattr(store, "advance")
    except Exception as exc:
        raise ReadOnlyCoordinatorError("invalid_store") from exc
    if not callable(get_current) or not callable(advance):
        raise ReadOnlyCoordinatorError("invalid_store")


def _validate_step_result_parts(
    before: object,
    platform_result: object,
    decision: object,
    after: object,
    persisted: object,
) -> None:
    _validate_delivery(before)
    _validate_delivery(after)
    if type(platform_result) is not PlatformResult:
        raise ReadOnlyCoordinatorError("invalid_step_result")
    try:
        PlatformResult.__post_init__(platform_result)
    except Exception as exc:
        raise ReadOnlyCoordinatorError("invalid_step_result") from exc
    if type(decision) is not ReconciliationDecision:
        raise ReadOnlyCoordinatorError("invalid_step_result")
    try:
        ReconciliationDecision.__post_init__(decision)
    except Exception as exc:
        raise ReadOnlyCoordinatorError("invalid_step_result") from exc
    if type(persisted) is not bool:
        raise ReadOnlyCoordinatorError("invalid_step_result")
    if decision.delivery != before:
        raise ReadOnlyCoordinatorError("invalid_step_result")
    if not _request_matches_delivery(platform_result.request, before):
        raise ReadOnlyCoordinatorError("invalid_step_result")
    if persisted:
        if decision.target is None:
            raise ReadOnlyCoordinatorError("invalid_step_result")
        if after.snapshot != decision.target or after.revision != before.revision + 1:
            raise ReadOnlyCoordinatorError("invalid_step_result")
    else:
        if decision.target is not None or after != before:
            raise ReadOnlyCoordinatorError("invalid_step_result")
    for field in _IDENTITY_FIELDS:
        if getattr(before.snapshot, field) != getattr(after.snapshot, field):
            raise ReadOnlyCoordinatorError("invalid_step_result")


@dataclass(frozen=True)
class ReadOnlyStepResult:
    """Immutable result of one coordinator step."""

    before: StoredDelivery
    platform_result: PlatformResult
    decision: ReconciliationDecision
    after: StoredDelivery
    persisted: bool

    def __post_init__(self) -> None:
        _validate_step_result_parts(
            self.before,
            self.platform_result,
            self.decision,
            self.after,
            self.persisted,
        )


class ReadOnlyDeliveryCoordinator:
    """Coordinate one read-only platform check and one optional CAS advance."""

    def __init__(
        self,
        store: object,
        adapters: Mapping[PlatformCapability, PlatformAdapter],
        *,
        timeout_seconds: float,
    ) -> None:
        _validate_store(store)
        _validate_timeout(timeout_seconds)
        if not isinstance(adapters, Mapping):
            raise ReadOnlyCoordinatorError("invalid_adapter_registry")
        configured = {}
        for capability, adapter in adapters.items():
            if type(capability) is not PlatformCapability:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            if capability is PlatformCapability.SEND_OFFER:
                raise ReadOnlyCoordinatorBlockedError("write_capability_not_allowed")
            if capability not in _READ_CAPABILITIES:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            try:
                declared = adapter.capabilities
                execute = adapter.execute
                declared_set = frozenset(declared)
            except Exception as exc:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch") from exc
            if not callable(execute) or capability not in declared_set:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            configured[capability] = adapter
        self._store = store
        self._adapters = MappingProxyType(configured)
        self._timeout_seconds = timeout_seconds

    def _read_current(self, delivery: StoredDelivery) -> None:
        try:
            persisted = self._store.get_by_purchase_id(delivery.snapshot.purchase_id)
        except AutoOfferStoreStaleWriteError as exc:
            raise ReadOnlyCoordinatorConflictError("store_read_conflict") from exc
        except AutoOfferStoreConflictError as exc:
            raise ReadOnlyCoordinatorConflictError("store_read_conflict") from exc
        except AutoOfferStoreError as exc:
            raise ReadOnlyCoordinatorError("store_read_failed") from exc
        except Exception as exc:
            raise ReadOnlyCoordinatorError("store_read_failed") from exc
        if type(persisted) is not StoredDelivery or persisted != delivery:
            raise ReadOnlyCoordinatorConflictError("persisted_delivery_mismatch")

    def _make_request(
        self,
        delivery: StoredDelivery,
        capability: PlatformCapability,
    ) -> PlatformRequest:
        snapshot = delivery.snapshot
        return PlatformRequest(
            purchase_id=snapshot.purchase_id,
            buff_order_id=snapshot.buff_order_id,
            account_id=snapshot.account_id,
            recipient_steam_id=snapshot.recipient_steam_id,
            revision=delivery.revision,
            capability=capability,
            timeout_seconds=self._timeout_seconds,
            steam_tradeoffer_id=(
                snapshot.steam_tradeoffer_id
                if capability in _TRADEOFFER_BOUND_CAPABILITIES
                else None
            ),
        )

    def _execute(
        self,
        adapter: object,
        request: PlatformRequest,
    ) -> PlatformResult:
        try:
            raw_result = adapter.execute(request)
        except Exception as exc:
            return _exception_result(request, exc)
        return _normalize_result(request, raw_result)

    def _plan(
        self,
        before: StoredDelivery,
        platform_result: PlatformResult,
    ) -> ReconciliationDecision:
        try:
            decision = plan_read_evidence_transition(before, platform_result)
        except Exception as exc:
            raise ReadOnlyCoordinatorError("planner_failed") from exc
        if type(decision) is not ReconciliationDecision:
            raise ReadOnlyCoordinatorError("planner_failed")
        return decision

    def _persist(
        self,
        before: StoredDelivery,
        decision: ReconciliationDecision,
        platform_result: PlatformResult,
    ) -> ReadOnlyStepResult:
        if decision.target is None:
            return ReadOnlyStepResult(
                before=before,
                platform_result=platform_result,
                decision=decision,
                after=before,
                persisted=False,
            )
        try:
            after = self._store.advance(decision.delivery, decision.target)
        except AutoOfferStoreStaleWriteError as exc:
            raise ReadOnlyCoordinatorConflictError("stale_write") from exc
        except AutoOfferStoreConflictError as exc:
            raise ReadOnlyCoordinatorConflictError("store_advance_conflict") from exc
        except AutoOfferStoreError as exc:
            raise ReadOnlyCoordinatorError("store_advance_failed") from exc
        except Exception as exc:
            raise ReadOnlyCoordinatorError("store_advance_failed") from exc
        try:
            return ReadOnlyStepResult(
                before=before,
                platform_result=platform_result,
                decision=decision,
                after=after,
                persisted=True,
            )
        except Exception as exc:
            raise ReadOnlyCoordinatorError("store_advance_invalid") from exc

    def step(self, delivery: StoredDelivery) -> ReadOnlyStepResult:
        """Execute at most one read and one planner-approved CAS advance."""
        _validate_delivery(delivery)
        self._read_current(delivery)
        capability = _required_capability(delivery)
        request = self._make_request(delivery, capability)
        adapter = self._adapters.get(capability)
        if adapter is None:
            platform_result = PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="adapter_not_available",
            )
        else:
            platform_result = self._execute(adapter, request)
        decision = self._plan(delivery, platform_result)
        return self._persist(delivery, decision, platform_result)


__all__ = [
    "ReadOnlyCoordinatorBlockedError",
    "ReadOnlyCoordinatorConflictError",
    "ReadOnlyCoordinatorError",
    "ReadOnlyDeliveryCoordinator",
    "ReadOnlyStepResult",
]
