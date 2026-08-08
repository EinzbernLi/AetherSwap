"""Fail-closed execution abstraction for native Auto Offer deliveries.

This module intentionally performs no platform work. It converts a validated
stored delivery into an immutable result that a future runtime may consume.
It never writes to :mod:`app.auto_offer.store`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from .contracts import (
    AutoOfferResult,
    DeliveryContractError,
    DeliveryStatus,
    result_blocks_next_purchase,
    validate_delivery_snapshot,
)
from .store import StoredDelivery


class DeliveryExecutorError(RuntimeError):
    """Raised when an execution request cannot be handled safely."""


@dataclass(frozen=True)
class DeliveryResult:
    """The side-effect-free outcome for one exact stored delivery revision."""

    delivery: StoredDelivery
    result: AutoOfferResult
    retryable: bool

    def __post_init__(self) -> None:
        _validate_stored_delivery(self.delivery)
        if type(self.result) is not AutoOfferResult:
            raise DeliveryExecutorError("result must be an AutoOfferResult")
        if type(self.retryable) is not bool:
            raise DeliveryExecutorError("retryable must be a bool")
        if self.retryable and self.result is not AutoOfferResult.WAITING:
            raise DeliveryExecutorError("only waiting results can be retried")

    @property
    def blocks_next_purchase(self) -> bool:
        """Whether this result must stop the host from buying another item."""
        return result_blocks_next_purchase(self.result)


class DeliveryExecutor(ABC):
    """Abstract, side-effect-free delivery execution boundary."""

    @abstractmethod
    def execute(self, delivery: StoredDelivery) -> DeliveryResult:
        """Return a fail-closed result for one stored delivery revision."""


def _validate_stored_delivery(delivery: object) -> None:
    if type(delivery) is not StoredDelivery:
        raise DeliveryExecutorError("delivery must be a StoredDelivery")
    if type(delivery.revision) is not int or delivery.revision < 1:
        raise DeliveryExecutorError("delivery revision must be a positive integer")
    try:
        validate_delivery_snapshot(delivery.snapshot)
    except DeliveryContractError as exc:
        raise DeliveryExecutorError("stored delivery violates the contract") from exc


def retry_is_allowed(previous: DeliveryResult, current: StoredDelivery) -> bool:
    """Allow retry only after a persisted revision advances from ``waiting``."""
    if type(previous) is not DeliveryResult:
        raise DeliveryExecutorError("previous must be a DeliveryResult")
    _validate_stored_delivery(current)
    if previous.result is not AutoOfferResult.WAITING or not previous.retryable:
        return False
    return (
        current.snapshot.purchase_id == previous.delivery.snapshot.purchase_id
        and current.revision > previous.delivery.revision
    )


_COMPLETE_STATUSES: Final[frozenset[DeliveryStatus]] = frozenset(
    {
        DeliveryStatus.RECEIVED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }
)


class MockDeliveryExecutor(DeliveryExecutor):
    """Deterministic local executor used only to exercise the abstraction."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, int], DeliveryResult] = {}

    def execute(self, delivery: StoredDelivery) -> DeliveryResult:
        _validate_stored_delivery(delivery)
        key = (delivery.snapshot.purchase_id, delivery.revision)
        cached = self._results.get(key)
        if cached is not None:
            if cached.delivery != delivery:
                raise DeliveryExecutorError(
                    "the same purchase revision cannot have two delivery snapshots"
                )
            return cached

        status = delivery.snapshot.delivery_status
        if status is DeliveryStatus.RESULT_UNKNOWN:
            outcome = AutoOfferResult.RESULT_UNKNOWN
        elif status is DeliveryStatus.BLOCKED:
            outcome = AutoOfferResult.BLOCKED
        elif status in _COMPLETE_STATUSES:
            outcome = AutoOfferResult.COMPLETE
        else:
            outcome = AutoOfferResult.WAITING

        result = DeliveryResult(
            delivery=delivery,
            result=outcome,
            retryable=outcome is AutoOfferResult.WAITING,
        )
        self._results[key] = result
        return result


__all__ = [
    "DeliveryExecutor",
    "DeliveryExecutorError",
    "DeliveryResult",
    "MockDeliveryExecutor",
    "retry_is_allowed",
]
