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
from typing import Final, Protocol, TypeAlias, runtime_checkable


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
    READ_STEAM_TRADE_OFFER = "read_steam_trade_offer"
    SEND_OFFER = "send_offer"


class PlatformResultStatus(str, Enum):
    """A normalized platform outcome; only SUCCESS proves success."""

    SUCCESS = "success"
    RESULT_UNKNOWN = "result_unknown"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    FAILURE = "failure"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class DeliveryDirectionEvidence:
    """Proof of the one delivery direction this boundary currently verifies."""

    direction: str = "seller_sends_offer"

    def __post_init__(self) -> None:
        if self.direction != "seller_sends_offer":
            raise PlatformAdapterProtocolError(
                "direction evidence must be seller_sends_offer"
            )


@dataclass(frozen=True)
class OfferStateEvidence:
    """The exact Steam offer ID proven for one canonical BUFF order."""

    steam_tradeoffer_id: str

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")


@dataclass(frozen=True)
class InventoryStateEvidence:
    """Canonical asset IDs from one readable recipient inventory snapshot."""

    assetids: tuple[str, ...]
    total_inventory_count: int | None = None

    def __post_init__(self) -> None:
        if type(self.assetids) is not tuple:
            raise PlatformAdapterProtocolError("assetids must be a tuple")
        for assetid in self.assetids:
            _require_id(assetid, "assetid")
        if len(set(self.assetids)) != len(self.assetids):
            raise PlatformAdapterProtocolError("assetids must not contain duplicates")
        if self.total_inventory_count is not None:
            if type(self.total_inventory_count) is not int or self.total_inventory_count < 0:
                raise PlatformAdapterProtocolError(
                    "total_inventory_count must be a non-negative integer or None"
                )
            if self.total_inventory_count < len(self.assetids):
                raise PlatformAdapterProtocolError(
                    "total_inventory_count cannot be smaller than assetids"
                )
        object.__setattr__(self, "assetids", tuple(sorted(self.assetids)))


class SteamTradeOfferLifecycle(str, Enum):
    """Steam Trade Offer states that this task can positively prove."""

    ACTIVE = "active"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class TradeOfferItemEvidence:
    """Minimal item-side evidence for one exact Steam Trade Offer."""

    appid: int
    contextid: str
    assetid: str
    amount: int

    def __post_init__(self) -> None:
        if type(self.appid) is not int or self.appid <= 0:
            raise PlatformAdapterProtocolError(
                "appid must be a positive integer"
            )
        _require_id(self.contextid, "contextid")
        _require_id(self.assetid, "assetid")
        if type(self.amount) is not int or self.amount <= 0:
            raise PlatformAdapterProtocolError(
                "amount must be a positive integer"
            )


@dataclass(frozen=True)
class SteamTradeOfferEvidence:
    """Typed evidence for one exact Steam Trade Offer read."""

    steam_tradeoffer_id: str
    account_steam_id: str
    counterparty_steam_id: str
    is_our_offer: bool
    lifecycle: SteamTradeOfferLifecycle
    items_to_give: tuple[TradeOfferItemEvidence, ...]
    items_to_receive: tuple[TradeOfferItemEvidence, ...]

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")
        _require_id(self.account_steam_id, "account_steam_id")
        _require_id(self.counterparty_steam_id, "counterparty_steam_id")
        if self.account_steam_id == self.counterparty_steam_id:
            raise PlatformAdapterProtocolError(
                "account and counterparty Steam IDs must differ"
            )
        if type(self.is_our_offer) is not bool:
            raise PlatformAdapterProtocolError("is_our_offer must be a bool")
        if type(self.lifecycle) is not SteamTradeOfferLifecycle:
            raise PlatformAdapterProtocolError(
                "lifecycle must be a SteamTradeOfferLifecycle"
            )
        for field, items in (
            ("items_to_give", self.items_to_give),
            ("items_to_receive", self.items_to_receive),
        ):
            if type(items) is not tuple:
                raise PlatformAdapterProtocolError(f"{field} must be a tuple")
            for item in items:
                if type(item) is not TradeOfferItemEvidence:
                    raise PlatformAdapterProtocolError(
                        f"{field} must contain TradeOfferItemEvidence"
                    )
                try:
                    TradeOfferItemEvidence.__post_init__(item)
                except PlatformAdapterProtocolError:
                    raise
                except Exception as error:
                    raise PlatformAdapterProtocolError(
                        f"{field} contains malformed item"
                    ) from error
            identities = [
                (item.appid, item.contextid, item.assetid) for item in items
            ]
            if len(set(identities)) != len(identities):
                raise PlatformAdapterProtocolError(
                    f"{field} contains duplicate item identity"
                )
        if not self.items_to_give and not self.items_to_receive:
            raise PlatformAdapterProtocolError(
                "both item sides cannot be empty"
            )
        for field in ("items_to_give", "items_to_receive"):
            items = getattr(self, field)
            object.__setattr__(
                self,
                field,
                tuple(
                    sorted(
                        items,
                        key=lambda item: (
                            item.appid,
                            item.contextid,
                            item.assetid,
                            item.amount,
                        ),
                    )
                ),
            )


PlatformEvidence: TypeAlias = (
    DeliveryDirectionEvidence
    | OfferStateEvidence
    | InventoryStateEvidence
    | SteamTradeOfferEvidence
)


def _require_id(value: object, field: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")


def _require_timeout(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise PlatformAdapterProtocolError("timeout_seconds must be a finite positive number")


_MISSING = object()


def _request_attribute(request: object, field: str) -> object:
    value = getattr(request, field, _MISSING)
    if value is _MISSING:
        raise PlatformAdapterProtocolError(
            f"request is missing {field}"
        )
    return value


def _validate_platform_request(request: object) -> None:
    for field in (
        "purchase_id",
        "buff_order_id",
        "account_id",
        "recipient_steam_id",
    ):
        _require_id(_request_attribute(request, field), field)
    revision = _request_attribute(request, "revision")
    if type(revision) is not int or revision < 1:
        raise PlatformAdapterProtocolError(
            "revision must be an integer of at least one"
        )
    capability = _request_attribute(request, "capability")
    if type(capability) is not PlatformCapability:
        raise PlatformAdapterProtocolError(
            "capability must be a PlatformCapability"
        )
    _require_timeout(_request_attribute(request, "timeout_seconds"))
    steam_tradeoffer_id = _request_attribute(request, "steam_tradeoffer_id")
    if capability is PlatformCapability.READ_STEAM_TRADE_OFFER:
        _require_id(steam_tradeoffer_id, "steam_tradeoffer_id")
    elif steam_tradeoffer_id is not None:
        raise PlatformAdapterProtocolError(
            "steam_tradeoffer_id is only valid for READ_STEAM_TRADE_OFFER"
        )


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
    steam_tradeoffer_id: str | None = None

    def __post_init__(self) -> None:
        _validate_platform_request(self)


@dataclass(frozen=True)
class PlatformResult:
    """Immutable normalized result for the exact request that produced it."""

    request: PlatformRequest
    status: PlatformResultStatus
    detail: str | None = None
    evidence: PlatformEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.request) is not PlatformRequest:
            raise PlatformAdapterProtocolError("result request must be a PlatformRequest")
        try:
            PlatformRequest.__post_init__(self.request)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "result request failed defensive validation"
            ) from error
        if type(self.status) is not PlatformResultStatus:
            raise PlatformAdapterProtocolError("result status must be a PlatformResultStatus")
        if self.detail is not None and (
            type(self.detail) is not str or not self.detail or self.detail.strip() != self.detail
        ):
            raise PlatformAdapterProtocolError("result detail must be a non-whitespace string")
        if self.status is not PlatformResultStatus.SUCCESS:
            if self.evidence is not None:
                raise PlatformAdapterProtocolError("non-success results cannot contain evidence")
            return
        expected_evidence = {
            PlatformCapability.READ_DELIVERY_DIRECTION: DeliveryDirectionEvidence,
            PlatformCapability.READ_OFFER_STATE: OfferStateEvidence,
            PlatformCapability.READ_INVENTORY_STATE: InventoryStateEvidence,
            PlatformCapability.READ_STEAM_TRADE_OFFER: SteamTradeOfferEvidence,
        }.get(self.request.capability)
        if expected_evidence is None:
            raise PlatformAdapterProtocolError("success is not allowed for this capability")
        if type(self.evidence) is not expected_evidence:
            raise PlatformAdapterProtocolError(
                "success results require matching capability evidence"
            )
        try:
            expected_evidence.__post_init__(self.evidence)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "success evidence failed defensive validation"
            ) from error

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
        PlatformRequest.__post_init__(request)
        if request.capability not in self._capabilities:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="capability_not_declared",
            )

        configured = self._outcomes.get(request, PlatformResultStatus.RESULT_UNKNOWN)
        if type(configured) is PlatformResult:
            try:
                configured_request = configured.request
                configured_status = configured.status
                configured_detail = configured.detail
                configured_evidence = configured.evidence
                PlatformRequest.__post_init__(configured_request)
                identity_matches = configured_request == request
            except Exception:
                return PlatformResult(
                    request=request,
                    status=PlatformResultStatus.MALFORMED,
                    detail="malformed_platform_result",
                )
            if not identity_matches:
                return PlatformResult(
                    request=request,
                    status=PlatformResultStatus.MALFORMED,
                    detail="result_identity_mismatch",
                )
            try:
                return PlatformResult(
                    request=request,
                    status=configured_status,
                    detail=configured_detail,
                    evidence=configured_evidence,
                )
            except PlatformAdapterProtocolError:
                detail = (
                    "success_evidence_required"
                    if configured_status
                    is PlatformResultStatus.SUCCESS
                    and configured_evidence is None
                    else "evidence_type_mismatch"
                )
                return PlatformResult(
                    request=request,
                    status=PlatformResultStatus.MALFORMED,
                    detail=detail,
                )
        if type(configured) is PlatformResultStatus:
            if configured is PlatformResultStatus.SUCCESS:
                return PlatformResult(
                    request=request,
                    status=PlatformResultStatus.MALFORMED,
                    detail="success_evidence_required",
                )
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
    "DeliveryDirectionEvidence",
    "FakePlatformAdapter",
    "InventoryStateEvidence",
    "OfferStateEvidence",
    "PlatformAdapter",
    "PlatformAdapterError",
    "PlatformAdapterProtocolError",
    "PlatformAdapterTimeoutError",
    "PlatformAdapterUnsupportedError",
    "PlatformCapability",
    "PlatformEvidence",
    "PlatformRequest",
    "PlatformResult",
    "PlatformResultStatus",
    "SteamTradeOfferEvidence",
    "SteamTradeOfferLifecycle",
    "TradeOfferItemEvidence",
]
