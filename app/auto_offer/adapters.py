"""Pure, fail-closed platform adapter boundary for Auto Offer delivery.

This module declares values that future Steam or BUFF adapters may implement.
It deliberately performs no I/O, platform action, persistence, or runtime
registration.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable


class PlatformAdapterError(RuntimeError):
    """Raised when an adapter request cannot be handled safely."""


class PlatformAdapterTimeoutError(PlatformAdapterError):
    """Represents an adapter timeout without claiming any platform outcome."""


class PlatformAdapterUnsupportedError(PlatformAdapterError):
    """Raised when an adapter does not declare a requested capability."""


class PlatformAdapterProtocolError(PlatformAdapterError):
    """Raised when a request or adapter value violates this boundary."""


class PlatformCapability(str, Enum):
    """Explicit operations a future platform adapter may declare."""

    READ_DELIVERY_DIRECTION = "read_delivery_direction"
    READ_OFFER_STATE = "read_offer_state"
    READ_INVENTORY_STATE = "read_inventory_state"
    SEND_OFFER = "send_offer"


class PlatformResultStatus(str, Enum):
    """A normalized platform outcome; only SUCCESS proves success."""

    SUCCESS = "success"
    RESULT_UNKNOWN = "result_unknown"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    FAILURE = "failure"
    MALFORMED = "malformed"


def _require_id(value: object, field: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")


def _require_timeout(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise PlatformAdapterProtocolError("timeout_seconds must be a finite positive number")


@dataclass(frozen=True)
class PlatformRequest:
    """Immutable exact identity and bounded wait budget for one platform call."""

    purchase_id: str
    buff_order_id: str
    account_id: str
    recipient_steam_id: str
    revision: int
    capability: PlatformCapability
    timeout_seconds: float

    def __post_init__(self) -> None:
        for field in (
            "purchase_id",
            "buff_order_id",
            "account_id",
            "recipient_steam_id",
        ):
            _require_id(getattr(self, field), field)
        if type(self.revision) is not int or self.revision < 1:
            raise PlatformAdapterProtocolError("revision must be an integer of at least one")
        if type(self.capability) is not PlatformCapability:
            raise PlatformAdapterProtocolError("capability must be a PlatformCapability")
        _require_timeout(self.timeout_seconds)


@dataclass(frozen=True)
class PlatformResult:
    """Immutable normalized result for the exact request that produced it."""

    request: PlatformRequest
    status: PlatformResultStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.request) is not PlatformRequest:
            raise PlatformAdapterProtocolError("result request must be a PlatformRequest")
        if type(self.status) is not PlatformResultStatus:
            raise PlatformAdapterProtocolError("result status must be a PlatformResultStatus")
        if self.detail is not None and (
            type(self.detail) is not str or not self.detail or self.detail.strip() != self.detail
        ):
            raise PlatformAdapterProtocolError("result detail must be a non-whitespace string")

    @property
    def is_success(self) -> bool:
        """Return true only for an explicitly normalized success."""
        return self.status is PlatformResultStatus.SUCCESS


@runtime_checkable
class PlatformAdapter(Protocol):
    """Minimal pure boundary that future real adapters may implement."""

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        """Return the explicit capabilities the adapter can handle."""

    def execute(self, request: PlatformRequest) -> PlatformResult:
        """Return a normalized, fail-closed result for one exact request."""


DEFAULT_PLATFORM_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_INVENTORY_STATE,
    }
)


class FakePlatformAdapter:
    """Deterministic local adapter used only to exercise the abstract boundary.

    Outcomes are keyed by the complete immutable request.  They may contain
    status values, normalized results, or exception/value sentinels so tests
    can prove that every unrecognized outcome remains fail closed.
    """

    def __init__(
        self,
        *,
        capabilities: Iterable[PlatformCapability] = DEFAULT_PLATFORM_CAPABILITIES,
        outcomes: Mapping[PlatformRequest, object] | None = None,
    ) -> None:
        declared = frozenset(capabilities)
        if any(type(item) is not PlatformCapability for item in declared):
            raise PlatformAdapterProtocolError("capabilities must contain PlatformCapability")
        if outcomes is not None and not isinstance(outcomes, Mapping):
            raise PlatformAdapterProtocolError("outcomes must be a mapping")
        configured = {} if outcomes is None else dict(outcomes)
        if any(type(request) is not PlatformRequest for request in configured):
            raise PlatformAdapterProtocolError("outcomes must use PlatformRequest keys")
        self._capabilities = declared
        self._outcomes = MappingProxyType(configured)

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        """Return the fixed local capability declaration."""
        return self._capabilities

    def execute(self, request: PlatformRequest) -> PlatformResult:
        """Return a deterministic local outcome without any platform action."""
        if type(request) is not PlatformRequest:
            raise PlatformAdapterProtocolError("request must be a PlatformRequest")
        if request.capability not in self._capabilities:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="capability_not_declared",
            )

        configured = self._outcomes.get(request, PlatformResultStatus.RESULT_UNKNOWN)
        if type(configured) is PlatformResult:
            if configured.request != request:
                return PlatformResult(
                    request=request,
                    status=PlatformResultStatus.MALFORMED,
                    detail="result_identity_mismatch",
                )
            return configured
        if type(configured) is PlatformResultStatus:
            return PlatformResult(request=request, status=configured)
        if isinstance(configured, PlatformAdapterTimeoutError):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.TIMEOUT,
                detail="simulated_timeout",
            )
        if isinstance(configured, PlatformAdapterUnsupportedError):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="simulated_unsupported",
            )
        if isinstance(configured, PlatformAdapterError):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.MALFORMED,
                detail="adapter_protocol_error",
            )
        if isinstance(configured, Exception):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.FAILURE,
                detail="adapter_internal_error",
            )
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.MALFORMED,
            detail="unrecognized_adapter_result",
        )


__all__ = [
    "DEFAULT_PLATFORM_CAPABILITIES",
    "FakePlatformAdapter",
    "PlatformAdapter",
    "PlatformAdapterError",
    "PlatformAdapterProtocolError",
    "PlatformAdapterTimeoutError",
    "PlatformAdapterUnsupportedError",
    "PlatformCapability",
    "PlatformRequest",
    "PlatformResult",
    "PlatformResultStatus",
]
