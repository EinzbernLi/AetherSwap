"""One-shot fail-closed platform step coordination.

The coordinator owns routing, adapter normalization, and contract-approved Store
CAS writes. Read reconciliation remains the authority for read-evidence targets;
non-idempotent SEND_OFFER and CONFIRM_OFFER writes persist durable attempt states
before their exact adapter invocation.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from .adapters import (
    ConfirmOfferEvidence,
    DeliveryDirectionEvidence,
    OfferStateEvidence,
    PlatformAdapter,
    PlatformAdapterError,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformAdapterUnsupportedError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SendOfferEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)
from .canary_authority import CanaryAuthorityError
from .contracts import (
    AutoOfferResult,
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
_WRITE_CAPABILITIES = frozenset(
    {
        PlatformCapability.SEND_OFFER,
        PlatformCapability.CONFIRM_OFFER,
    }
)
_TRADEOFFER_BOUND_CAPABILITIES = frozenset(
    {
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        PlatformCapability.CONFIRM_OFFER,
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
    """The current state or capability cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class _NormalSendProof:
    """Opaque one-shot proof of one exact fresh BUFF send-authority read."""

    purchase_id: str
    buff_order_id: str
    account_id: str
    recipient_steam_id: str
    revision: int

    def __repr__(self) -> str:
        return "<opaque normal send proof>"

    def __reduce__(self):
        raise TypeError("normal_send_proof_not_serializable")

    def __copy__(self):
        raise TypeError("normal_send_proof_not_serializable")

    def __deepcopy__(self, _memo):
        raise TypeError("normal_send_proof_not_serializable")


def _validate_timeout(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ReadOnlyCoordinatorError("invalid_timeout")


def _validate_clock(clock: object) -> None:
    if not callable(clock):
        raise ReadOnlyCoordinatorError("invalid_clock")


def _validate_timestamp(value: object) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ReadOnlyCoordinatorError("invalid_clock_value")
    return float(value)


def _validate_delivery(delivery: object) -> None:
    if type(delivery) is not StoredDelivery:
        raise ReadOnlyCoordinatorError("invalid_delivery")
    if type(delivery.revision) is not int or delivery.revision < 1:
        raise ReadOnlyCoordinatorError("invalid_delivery")
    try:
        validate_delivery_snapshot(delivery.snapshot)
    except DeliveryContractError as exc:
        raise ReadOnlyCoordinatorError("invalid_delivery") from exc


def _validate_trade_offer_expectations(
    counterparty_steam_id: object,
    is_our_offer: object,
) -> tuple[str | None, bool | None]:
    if counterparty_steam_id is None and is_our_offer is None:
        return None, None
    if (
        type(counterparty_steam_id) is not str
        or not counterparty_steam_id
        or counterparty_steam_id.strip() != counterparty_steam_id
        or any(ord(character) < 32 for character in counterparty_steam_id)
    ):
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_counterparty")
    if type(is_our_offer) is not bool:
        raise ReadOnlyCoordinatorError("invalid_expected_trade_offer_direction")
    return counterparty_steam_id, is_our_offer


def _same_delivery_identity(left: StoredDelivery, right: StoredDelivery) -> bool:
    return all(
        getattr(left.snapshot, field) == getattr(right.snapshot, field)
        for field in _IDENTITY_FIELDS
    )


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


def _required_read_capability(delivery: StoredDelivery) -> PlatformCapability:
    status = delivery.snapshot.delivery_status
    mode = delivery.snapshot.delivery_mode
    if status is DeliveryStatus.PENDING_DIRECTION:
        return PlatformCapability.READ_DELIVERY_DIRECTION
    if status is DeliveryStatus.AWAITING_OFFER:
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            raise ReadOnlyCoordinatorBlockedError("write_capability_required")
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return PlatformCapability.READ_OFFER_STATE
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            return PlatformCapability.READ_OFFER_STATE
    if status is DeliveryStatus.RESULT_UNKNOWN:
        snapshot = delivery.snapshot
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            if (
                snapshot.offer_attempted_at is not None
                and snapshot.steam_tradeoffer_id is None
            ):
                return PlatformCapability.READ_OFFER_STATE
            if snapshot.steam_tradeoffer_id is not None:
                return PlatformCapability.READ_STEAM_TRADE_OFFER
        if (
            mode is DeliveryMode.SELLER_SENDS_OFFER
            and snapshot.steam_tradeoffer_id is not None
        ):
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status is DeliveryStatus.AWAITING_INVENTORY:
        return PlatformCapability.READ_STEAM_COMPLETED_TRADE
    if status is DeliveryStatus.OFFER_RECEIVED:
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status in {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
    }:
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status is DeliveryStatus.OFFER_CONFIRMED:
        if mode in {
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryMode.BUYER_SENDS_OFFER,
        }:
            return PlatformCapability.READ_STEAM_TRADE_OFFER
    if status in {
        DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        DeliveryStatus.OFFER_TERMINATED,
    }:
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
    """Immutable result of one read-side coordinator step."""

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


@dataclass(frozen=True)
class SendOfferStepResult:
    """Immutable result of one durably recorded SEND_OFFER attempt."""

    before: StoredDelivery
    attempted: StoredDelivery
    platform_result: PlatformResult
    after: StoredDelivery

    def __post_init__(self) -> None:
        try:
            _validate_delivery(self.before)
            _validate_delivery(self.attempted)
            _validate_delivery(self.after)
            PlatformResult.__post_init__(self.platform_result)
        except Exception as exc:
            raise ReadOnlyCoordinatorError("invalid_send_step_result") from exc
        if (
            self.before.snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or self.before.snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER
            or self.attempted.snapshot.delivery_status
            is not DeliveryStatus.OFFER_ATTEMPTED
            or self.attempted.revision != self.before.revision + 1
            or not _same_delivery_identity(self.before, self.attempted)
            or not _same_delivery_identity(self.attempted, self.after)
            or not _request_matches_delivery(
                self.platform_result.request, self.attempted
            )
            or self.platform_result.request.capability
            is not PlatformCapability.SEND_OFFER
            or self.after.revision != self.attempted.revision + 1
        ):
            raise ReadOnlyCoordinatorError("invalid_send_step_result")
        if self.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT:
            if (
                self.platform_result.status is not PlatformResultStatus.SUCCESS
                or type(self.platform_result.evidence) is not SendOfferEvidence
                or self.after.snapshot.steam_tradeoffer_id
                != self.platform_result.evidence.steam_tradeoffer_id
            ):
                raise ReadOnlyCoordinatorError("invalid_send_step_result")
        elif self.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            if self.after.snapshot.delivery_error != "write_result_unknown":
                raise ReadOnlyCoordinatorError("invalid_send_step_result")
        else:
            raise ReadOnlyCoordinatorError("invalid_send_step_result")


@dataclass(frozen=True)
class ConfirmOfferStepResult:
    """Immutable result of one durably recorded exact confirmation attempt."""

    before: StoredDelivery
    attempted: StoredDelivery
    platform_result: PlatformResult
    after: StoredDelivery

    def __post_init__(self) -> None:
        try:
            _validate_delivery(self.before)
            _validate_delivery(self.attempted)
            _validate_delivery(self.after)
            PlatformResult.__post_init__(self.platform_result)
        except Exception as exc:
            raise ReadOnlyCoordinatorError("invalid_confirm_step_result") from exc
        if (
            self.before.snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or self.before.snapshot.delivery_status
            is not DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
            or self.attempted.snapshot.delivery_status
            is not DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
            or self.attempted.revision != self.before.revision + 1
            or not _same_delivery_identity(self.before, self.attempted)
            or not _same_delivery_identity(self.attempted, self.after)
            or not _request_matches_delivery(
                self.platform_result.request, self.attempted
            )
            or self.platform_result.request.capability
            is not PlatformCapability.CONFIRM_OFFER
        ):
            raise ReadOnlyCoordinatorError("invalid_confirm_step_result")
        if self.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED:
            if (
                self.after.revision != self.attempted.revision + 1
                or self.platform_result.status is not PlatformResultStatus.SUCCESS
                or type(self.platform_result.evidence) is not ConfirmOfferEvidence
                or self.after.snapshot.steam_tradeoffer_id
                != self.platform_result.evidence.steam_tradeoffer_id
            ):
                raise ReadOnlyCoordinatorError("invalid_confirm_step_result")
        elif self.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            if (
                self.after.revision != self.attempted.revision + 1
                or self.platform_result.status is not PlatformResultStatus.RESULT_UNKNOWN
                or self.after.snapshot.delivery_error != "write_result_unknown"
            ):
                raise ReadOnlyCoordinatorError("invalid_confirm_step_result")
        elif self.after == self.attempted:
            if self.platform_result.status not in {
                PlatformResultStatus.UNSUPPORTED,
                PlatformResultStatus.TIMEOUT,
                PlatformResultStatus.FAILURE,
                PlatformResultStatus.MALFORMED,
            }:
                raise ReadOnlyCoordinatorError("invalid_confirm_step_result")
        else:
            raise ReadOnlyCoordinatorError("invalid_confirm_step_result")


class DeliveryCoordinator:
    """Coordinate one exact platform step and contract-approved Store CAS writes."""

    def __init__(
        self,
        store: object,
        adapters: Mapping[PlatformCapability, PlatformAdapter],
        *,
        timeout_seconds: float,
        allow_writes: bool = False,
        allow_confirmation_writes: bool = False,
        write_guard=None,
        expected_trade_offer_counterparty_steam_id: str | None = None,
        expected_trade_offer_is_our_offer: bool | None = None,
        clock=None,
    ) -> None:
        _validate_store(store)
        _validate_timeout(timeout_seconds)
        if type(allow_writes) is not bool:
            raise ReadOnlyCoordinatorError("invalid_allow_writes")
        if type(allow_confirmation_writes) is not bool:
            raise ReadOnlyCoordinatorError("invalid_allow_confirmation_writes")
        if allow_confirmation_writes and not allow_writes:
            raise ReadOnlyCoordinatorError("confirmation_writes_require_allow_writes")
        if write_guard is not None and not callable(write_guard):
            raise ReadOnlyCoordinatorError("invalid_write_guard")
        expected_counterparty, expected_direction = _validate_trade_offer_expectations(
            expected_trade_offer_counterparty_steam_id,
            expected_trade_offer_is_our_offer,
        )
        actual_clock = time.time if clock is None else clock
        _validate_clock(actual_clock)
        if not isinstance(adapters, Mapping):
            raise ReadOnlyCoordinatorError("invalid_adapter_registry")
        configured = {}
        for capability, adapter in adapters.items():
            if type(capability) is not PlatformCapability:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            if capability is PlatformCapability.SEND_OFFER and not allow_writes:
                raise ReadOnlyCoordinatorBlockedError("write_capability_not_allowed")
            if capability is PlatformCapability.CONFIRM_OFFER and (
                not allow_writes or not allow_confirmation_writes
            ):
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            if capability not in _READ_CAPABILITIES and capability not in _WRITE_CAPABILITIES:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            try:
                declared = adapter.capabilities
                execute = adapter.execute
                declared_set = frozenset(declared)
            except Exception as exc:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch") from exc
            if not callable(execute) or capability not in declared_set:
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            if capability in _WRITE_CAPABILITIES and declared_set != frozenset({capability}):
                raise ReadOnlyCoordinatorError("adapter_capability_mismatch")
            configured[capability] = adapter
        self._store = store
        self._adapters = MappingProxyType(configured)
        self._timeout_seconds = timeout_seconds
        self._allow_writes = allow_writes
        self._allow_confirmation_writes = allow_confirmation_writes
        self._write_guard = write_guard
        self._expected_trade_offer_counterparty_steam_id = expected_counterparty
        self._expected_trade_offer_is_our_offer = expected_direction
        self._normal_send_proof: _NormalSendProof | None = None
        self._confirmation_identity_proof: tuple[str, str, int, str] | None = None
        self._clock = actual_clock

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

    @staticmethod
    def _confirmation_proof_key(delivery: StoredDelivery) -> tuple[str, str, int, str]:
        tradeoffer_id = delivery.snapshot.steam_tradeoffer_id
        if type(tradeoffer_id) is not str or not tradeoffer_id:
            raise ReadOnlyCoordinatorBlockedError("confirmation_trade_offer_required")
        return (
            delivery.snapshot.purchase_id,
            delivery.snapshot.buff_order_id,
            delivery.revision,
            tradeoffer_id,
        )

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception:
            raise ReadOnlyCoordinatorError("clock_failed") from None
        return _validate_timestamp(value)

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
        if request.capability in _WRITE_CAPABILITIES and self._write_guard is not None:
            try:
                guard = self._write_guard(request)
                with guard:
                    try:
                        raw_result = adapter.execute(request)
                    except Exception as exc:
                        return _exception_result(request, exc)
            except CanaryAuthorityError as exc:
                raise ReadOnlyCoordinatorBlockedError("canary_write_blocked") from exc
            except ReadOnlyCoordinatorError:
                raise
            except Exception as exc:
                raise ReadOnlyCoordinatorBlockedError("write_guard_failed") from exc
            return _normalize_result(request, raw_result)
        try:
            raw_result = adapter.execute(request)
        except Exception as exc:
            return _exception_result(request, exc)
        return _normalize_result(request, raw_result)

    def _guard_trade_offer_identity(
        self,
        delivery: StoredDelivery,
        platform_result: PlatformResult,
    ) -> PlatformResult:
        durable_counterparty = delivery.snapshot.counterparty_steam_id
        configured_counterparty = self._expected_trade_offer_counterparty_steam_id
        if (
            durable_counterparty is not None
            and configured_counterparty is not None
            and durable_counterparty != configured_counterparty
        ):
            return PlatformResult(
                request=platform_result.request,
                status=PlatformResultStatus.FAILURE,
                detail="identity_mismatch",
            )
        expected_counterparty = durable_counterparty or configured_counterparty
        expected_direction = self._expected_trade_offer_is_our_offer
        delivery_direction = (
            delivery.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
        )
        if expected_direction is None and durable_counterparty is not None:
            expected_direction = delivery_direction
        if (
            expected_counterparty is None
            or platform_result.request.capability
            is not PlatformCapability.READ_STEAM_TRADE_OFFER
            or platform_result.status is not PlatformResultStatus.SUCCESS
        ):
            return platform_result
        evidence = platform_result.evidence
        if (
            type(evidence) is not SteamTradeOfferEvidence
            or evidence.steam_tradeoffer_id
            != platform_result.request.steam_tradeoffer_id
            or evidence.account_steam_id != delivery.snapshot.recipient_steam_id
            or evidence.counterparty_steam_id != expected_counterparty
            or evidence.is_our_offer is not expected_direction
            or expected_direction is not delivery_direction
        ):
            return PlatformResult(
                request=platform_result.request,
                status=PlatformResultStatus.FAILURE,
                detail="identity_mismatch",
            )
        return platform_result

    @staticmethod
    def _normal_send_proof_key(
        delivery: StoredDelivery,
    ) -> tuple[str, str, str, str, int]:
        snapshot = delivery.snapshot
        return (
            snapshot.purchase_id,
            snapshot.buff_order_id,
            snapshot.account_id,
            snapshot.recipient_steam_id,
            delivery.revision,
        )

    def _plan(
        self,
        before: StoredDelivery,
        platform_result: PlatformResult,
    ) -> ReconciliationDecision:
        observed_at = None
        if (
            before.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
            and before.snapshot.delivery_status in {
                DeliveryStatus.OFFER_ATTEMPTED,
                DeliveryStatus.RESULT_UNKNOWN,
            }
            and before.snapshot.steam_tradeoffer_id is None
            and platform_result.status is PlatformResultStatus.SUCCESS
            and platform_result.request.capability is PlatformCapability.READ_OFFER_STATE
            and type(platform_result.evidence) is OfferStateEvidence
        ):
            observed_at = self._now()
        try:
            decision = plan_read_evidence_transition(
                before,
                platform_result,
                observed_at=observed_at,
            )
        except Exception as exc:
            raise ReadOnlyCoordinatorError("planner_failed") from exc
        if type(decision) is not ReconciliationDecision:
            raise ReadOnlyCoordinatorError("planner_failed")
        return decision

    def _advance(
        self,
        current: StoredDelivery,
        target,
    ) -> StoredDelivery:
        try:
            return self._store.advance(current, target)
        except AutoOfferStoreStaleWriteError as exc:
            raise ReadOnlyCoordinatorConflictError("stale_write") from exc
        except AutoOfferStoreConflictError as exc:
            raise ReadOnlyCoordinatorConflictError("store_advance_conflict") from exc
        except AutoOfferStoreError as exc:
            raise ReadOnlyCoordinatorError("store_advance_failed") from exc
        except Exception as exc:
            raise ReadOnlyCoordinatorError("store_advance_failed") from exc

    def _persist_read(
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
        after = self._advance(decision.delivery, decision.target)
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

    def _persist_result_unknown(
        self,
        attempted: StoredDelivery,
    ) -> StoredDelivery:
        target = replace(
            attempted.snapshot,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            delivery_error="write_result_unknown",
        )
        return self._advance(attempted, target)

    def _step_read(self, delivery: StoredDelivery) -> ReadOnlyStepResult:
        capability = _required_read_capability(delivery)
        request = self._make_request(delivery, capability)
        adapter = self._adapters.get(capability)
        if adapter is None:
            if (
                capability is PlatformCapability.READ_OFFER_STATE
                and delivery.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
                and delivery.snapshot.delivery_status in {
                    DeliveryStatus.OFFER_ATTEMPTED,
                    DeliveryStatus.RESULT_UNKNOWN,
                }
            ):
                raise ReadOnlyCoordinatorBlockedError("read_step_not_available")
            platform_result = PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="adapter_not_available",
            )
        else:
            platform_result = self._execute(adapter, request)
        platform_result = self._guard_trade_offer_identity(delivery, platform_result)
        decision = self._plan(delivery, platform_result)
        return self._persist_read(delivery, decision, platform_result)

    def read_send_authority(self, delivery: StoredDelivery) -> _NormalSendProof:
        """Mint one process-local proof from one exact fresh BUFF waiting read."""

        self._normal_send_proof = None
        self._confirmation_identity_proof = None
        _validate_delivery(delivery)
        self._read_current(delivery)
        snapshot = delivery.snapshot
        if (
            snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
        ):
            raise ReadOnlyCoordinatorBlockedError("send_authority_not_available")
        adapter = self._adapters.get(PlatformCapability.READ_DELIVERY_DIRECTION)
        if adapter is None:
            raise ReadOnlyCoordinatorBlockedError("send_authority_adapter_required")
        request = self._make_request(
            delivery,
            PlatformCapability.READ_DELIVERY_DIRECTION,
        )
        platform_result = self._execute(adapter, request)
        evidence = platform_result.evidence
        if (
            platform_result.status is not PlatformResultStatus.SUCCESS
            or type(evidence) is not DeliveryDirectionEvidence
            or evidence.direction != "buyer_sends_offer"
            or evidence.counterparty_steam_id is not None
        ):
            raise ReadOnlyCoordinatorBlockedError("send_authority_not_proven")
        proof = _NormalSendProof(*self._normal_send_proof_key(delivery))
        self._normal_send_proof = proof
        return proof

    def recover_result_unknown_readonly(
        self,
        delivery: StoredDelivery,
    ) -> ReadOnlyStepResult:
        """Perform only exact BUFF read recovery for one unbound buyer SEND."""

        self._normal_send_proof = None
        self._confirmation_identity_proof = None
        _validate_delivery(delivery)
        self._read_current(delivery)
        snapshot = delivery.snapshot
        if (
            snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
            or snapshot.offer_attempted_at is None
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
        ):
            raise ReadOnlyCoordinatorBlockedError(
                "result_unknown_read_recovery_not_available"
            )
        capability = PlatformCapability.READ_OFFER_STATE
        adapter = self._adapters.get(capability)
        if adapter is None:
            raise ReadOnlyCoordinatorBlockedError("read_step_not_available")
        request = self._make_request(delivery, capability)
        platform_result = self._execute(adapter, request)
        decision = self._plan(delivery, platform_result)
        return self._persist_read(delivery, decision, platform_result)

    def read_confirmation_state(self, delivery: StoredDelivery) -> ReadOnlyStepResult:
        """Read and mint one process-local proof for the next exact confirmation."""
        self._normal_send_proof = None
        self._confirmation_identity_proof = None
        _validate_delivery(delivery)
        self._read_current(delivery)
        if (
            self._expected_trade_offer_counterparty_steam_id is None
            or self._expected_trade_offer_is_our_offer is None
        ):
            raise ReadOnlyCoordinatorBlockedError("confirmation_identity_guard_required")
        if (
            delivery.snapshot.delivery_status
            is not DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
            or delivery.snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
        ):
            raise ReadOnlyCoordinatorBlockedError("confirmation_read_not_available")
        result = self._step_read(delivery)
        evidence = result.platform_result.evidence
        if (
            result.persisted is False
            and result.after == delivery
            and result.decision.result is AutoOfferResult.WAITING
            and result.platform_result.status is PlatformResultStatus.SUCCESS
            and type(evidence) is SteamTradeOfferEvidence
            and evidence.lifecycle
            is SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
            and evidence.items_to_give == ()
        ):
            self._confirmation_identity_proof = self._confirmation_proof_key(delivery)
        return result

    def _step_send_offer(
        self,
        delivery: StoredDelivery,
        *,
        allow_success_binding: bool,
    ) -> SendOfferStepResult:
        if not self._allow_writes:
            raise ReadOnlyCoordinatorBlockedError("write_capability_required")
        adapter = self._adapters.get(PlatformCapability.SEND_OFFER)
        if adapter is None:
            raise ReadOnlyCoordinatorBlockedError("send_offer_adapter_required")

        attempted_at = self._now()
        attempted_target = replace(
            delivery.snapshot,
            delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
            offer_attempted_at=attempted_at,
        )
        attempted = self._advance(delivery, attempted_target)

        request = self._make_request(attempted, PlatformCapability.SEND_OFFER)
        platform_result = self._execute(adapter, request)
        if (
            allow_success_binding
            and platform_result.status is PlatformResultStatus.SUCCESS
            and type(platform_result.evidence) is SendOfferEvidence
        ):
            try:
                sent_at = self._now()
            except ReadOnlyCoordinatorError:
                after = self._persist_result_unknown(attempted)
                return SendOfferStepResult(
                    before=delivery,
                    attempted=attempted,
                    platform_result=platform_result,
                    after=after,
                )
            if sent_at < attempted_at:
                after = self._persist_result_unknown(attempted)
                return SendOfferStepResult(
                    before=delivery,
                    attempted=attempted,
                    platform_result=platform_result,
                    after=after,
                )
            sent_target = replace(
                attempted.snapshot,
                delivery_status=DeliveryStatus.OFFER_SENT,
                steam_tradeoffer_id=platform_result.evidence.steam_tradeoffer_id,
                offer_sent_at=sent_at,
            )
            after = self._advance(attempted, sent_target)
        else:
            after = self._persist_result_unknown(attempted)
        return SendOfferStepResult(
            before=delivery,
            attempted=attempted,
            platform_result=platform_result,
            after=after,
        )

    def send_offer_with_authority(
        self,
        delivery: StoredDelivery,
        proof: object,
    ) -> SendOfferStepResult:
        """Consume one exact normal proof and perform one unknown-result SEND."""

        current_proof = self._normal_send_proof
        self._normal_send_proof = None
        self._confirmation_identity_proof = None
        _validate_delivery(delivery)
        if proof is not current_proof or type(proof) is not _NormalSendProof:
            raise ReadOnlyCoordinatorBlockedError("send_authority_proof_required")
        self._read_current(delivery)
        if (
            self._normal_send_proof_key(delivery)
            != (
                proof.purchase_id,
                proof.buff_order_id,
                proof.account_id,
                proof.recipient_steam_id,
                proof.revision,
            )
        ):
            raise ReadOnlyCoordinatorBlockedError("send_authority_proof_mismatch")
        snapshot = delivery.snapshot
        if (
            snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
        ):
            raise ReadOnlyCoordinatorBlockedError("send_authority_not_available")
        return self._step_send_offer(delivery, allow_success_binding=False)

    def _step_confirm_offer(self, delivery: StoredDelivery) -> ConfirmOfferStepResult:
        if not self._allow_writes or not self._allow_confirmation_writes:
            raise ReadOnlyCoordinatorBlockedError("confirmation_write_capability_required")
        if self._expected_trade_offer_counterparty_steam_id is not None:
            proof = self._confirmation_identity_proof
            self._confirmation_identity_proof = None
            if proof != self._confirmation_proof_key(delivery):
                raise ReadOnlyCoordinatorBlockedError("confirmation_identity_proof_required")
        adapter = self._adapters.get(PlatformCapability.CONFIRM_OFFER)
        if adapter is None:
            raise ReadOnlyCoordinatorBlockedError("confirm_offer_adapter_required")

        attempted_target = replace(
            delivery.snapshot,
            delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        )
        attempted = self._advance(delivery, attempted_target)

        request = self._make_request(attempted, PlatformCapability.CONFIRM_OFFER)
        platform_result = self._execute(adapter, request)
        if (
            platform_result.status is PlatformResultStatus.SUCCESS
            and type(platform_result.evidence) is ConfirmOfferEvidence
        ):
            confirmed_target = replace(
                attempted.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMED,
            )
            after = self._advance(attempted, confirmed_target)
        elif platform_result.status is PlatformResultStatus.RESULT_UNKNOWN:
            after = self._persist_result_unknown(attempted)
        else:
            after = attempted
        return ConfirmOfferStepResult(
            before=delivery,
            attempted=attempted,
            platform_result=platform_result,
            after=after,
        )

    def step(
        self,
        delivery: StoredDelivery,
    ) -> ReadOnlyStepResult | SendOfferStepResult | ConfirmOfferStepResult:
        """Execute one read step or one explicitly enabled crash-safe write attempt."""
        self._normal_send_proof = None
        _validate_delivery(delivery)
        self._read_current(delivery)
        if (
            delivery.snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
            and delivery.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
        ):
            self._confirmation_identity_proof = None
            if (
                self._expected_trade_offer_counterparty_steam_id is None
                or self._expected_trade_offer_is_our_offer is None
            ):
                raise ReadOnlyCoordinatorBlockedError(
                    "normal_send_authority_required"
                )
            return self._step_send_offer(delivery, allow_success_binding=True)
        if (
            self._allow_confirmation_writes
            and delivery.snapshot.delivery_status
            is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
            and delivery.snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
        ):
            return self._step_confirm_offer(delivery)
        self._confirmation_identity_proof = None
        return self._step_read(delivery)


ReadOnlyDeliveryCoordinator = DeliveryCoordinator
DeliveryReadStepResult = ReadOnlyStepResult


__all__ = [
    "ConfirmOfferStepResult",
    "DeliveryCoordinator",
    "DeliveryReadStepResult",
    "ReadOnlyCoordinatorBlockedError",
    "ReadOnlyCoordinatorConflictError",
    "ReadOnlyCoordinatorError",
    "ReadOnlyDeliveryCoordinator",
    "ReadOnlyStepResult",
    "SendOfferStepResult",
]
