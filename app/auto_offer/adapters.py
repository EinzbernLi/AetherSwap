"""Pure, fail-closed platform adapter boundary for Auto Offer delivery.

This module declares values that Steam or BUFF adapters may implement. It
performs no I/O, platform action, persistence, or runtime registration.
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
    """Explicit operations a platform adapter may declare."""

    READ_DELIVERY_DIRECTION = "read_delivery_direction"
    READ_OFFER_STATE = "read_offer_state"
    READ_INVENTORY_STATE = "read_inventory_state"
    READ_STEAM_TRADE_OFFER = "read_steam_trade_offer"
    READ_STEAM_COMPLETED_TRADE = "read_steam_completed_trade"
    SEND_OFFER = "send_offer"
    ACCEPT_OFFER = "accept_offer"
    CONFIRM_OFFER = "confirm_offer"


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
    """Proof of the exact delivery direction for one canonical order."""

    direction: str = "seller_sends_offer"
    counterparty_steam_id: str | None = None

    def __post_init__(self) -> None:
        if self.direction not in {"seller_sends_offer", "buyer_sends_offer"}:
            raise PlatformAdapterProtocolError(
                "direction evidence must be seller_sends_offer or buyer_sends_offer"
            )
        if self.counterparty_steam_id is not None:
            _require_id(self.counterparty_steam_id, "counterparty_steam_id")
        if (
            self.direction == "buyer_sends_offer"
            and self.counterparty_steam_id is not None
        ):
            raise PlatformAdapterProtocolError(
                "buyer direction cannot carry seller counterparty evidence"
            )


@dataclass(frozen=True)
class OfferStateEvidence:
    """The exact Steam offer ID proven for one canonical BUFF order."""

    steam_tradeoffer_id: str

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")


@dataclass(frozen=True)
class SendOfferEvidence:
    """The exact Steam offer ID proven by one SEND_OFFER invocation."""

    steam_tradeoffer_id: str

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")


@dataclass(frozen=True)
class AcceptOfferEvidence:
    """Proof that one exact incoming Steam Trade Offer ACCEPT returned success."""

    steam_tradeoffer_id: str
    account_steam_id: str

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")
        _require_id(self.account_steam_id, "account_steam_id")


@dataclass(frozen=True)
class ConfirmOfferEvidence:
    """Proof that one exact Steam Trade Offer mobile confirmation succeeded."""

    steam_tradeoffer_id: str
    account_steam_id: str

    def __post_init__(self) -> None:
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")
        _require_id(self.account_steam_id, "account_steam_id")


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
    """Steam Trade Offer states that exact reads can positively prove."""

    ACTIVE = "active"
    ACCEPTED = "accepted"
    CREATED_NEEDS_CONFIRMATION = "created_needs_confirmation"
    COUNTERED = "countered"
    EXPIRED = "expired"
    CANCELED = "canceled"
    DECLINED = "declined"
    INVALID_ITEMS = "invalid_items"
    CANCELED_BY_SECOND_FACTOR = "canceled_by_second_factor"
    IN_ESCROW = "in_escrow"

    @property
    def is_terminal_without_trade(self) -> bool:
        return self in {
            SteamTradeOfferLifecycle.COUNTERED,
            SteamTradeOfferLifecycle.EXPIRED,
            SteamTradeOfferLifecycle.CANCELED,
            SteamTradeOfferLifecycle.DECLINED,
            SteamTradeOfferLifecycle.INVALID_ITEMS,
            SteamTradeOfferLifecycle.CANCELED_BY_SECOND_FACTOR,
        }


@dataclass(frozen=True)
class TradeOfferItemEvidence:
    """Minimal item-side evidence for one exact Steam Trade Offer."""

    appid: int
    contextid: str
    assetid: str
    amount: int

    def __post_init__(self) -> None:
        if type(self.appid) is not int or self.appid <= 0:
            raise PlatformAdapterProtocolError("appid must be a positive integer")
        _require_id(self.contextid, "contextid")
        _require_id(self.assetid, "assetid")
        if type(self.amount) is not int or self.amount <= 0:
            raise PlatformAdapterProtocolError("amount must be a positive integer")


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
            identities = [(item.appid, item.contextid, item.assetid) for item in items]
            if len(set(identities)) != len(identities):
                raise PlatformAdapterProtocolError(
                    f"{field} contains duplicate item identity"
                )
        if not self.items_to_give and not self.items_to_receive:
            raise PlatformAdapterProtocolError("both item sides cannot be empty")
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


@dataclass(frozen=True)
class CompletedTradeItemEvidence:
    """Minimal source-to-post-trade identity evidence for one item."""

    appid: int
    contextid: str
    assetid: str
    amount: int
    new_contextid: str
    new_assetid: str

    def __post_init__(self) -> None:
        try:
            _validate_completed_trade_item(self)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "completed trade item failed validation"
            ) from error


@dataclass(frozen=True)
class RecipientInventoryItemEvidence:
    """One exact item identity observed in the recipient inventory snapshot."""

    appid: int
    contextid: str
    assetid: str
    amount: int

    def __post_init__(self) -> None:
        try:
            _validate_recipient_inventory_item(self)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "recipient inventory item failed validation"
            ) from error


@dataclass(frozen=True)
class SteamCompletedTradeEvidence:
    """Typed evidence for one exact completed trade and recipient snapshot."""

    steam_tradeoffer_id: str
    steam_trade_id: str
    account_steam_id: str
    counterparty_steam_id: str
    completed_at: float
    items_given: tuple[CompletedTradeItemEvidence, ...]
    items_received: tuple[CompletedTradeItemEvidence, ...]
    inventory_confirmed_items: tuple[RecipientInventoryItemEvidence, ...]

    def __post_init__(self) -> None:
        try:
            _validate_steam_completed_trade_evidence(self)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "completed trade evidence failed validation"
            ) from error


PlatformEvidence: TypeAlias = (
    DeliveryDirectionEvidence
    | OfferStateEvidence
    | SendOfferEvidence
    | AcceptOfferEvidence
    | ConfirmOfferEvidence
    | InventoryStateEvidence
    | SteamTradeOfferEvidence
    | SteamCompletedTradeEvidence
)


def _require_id(value: object, field: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")


def _require_timeout(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise PlatformAdapterProtocolError(
            "timeout_seconds must be a finite positive number"
        )


def _validate_completed_trade_item(item: object) -> None:
    appid = getattr(item, "appid", _MISSING)
    contextid = getattr(item, "contextid", _MISSING)
    assetid = getattr(item, "assetid", _MISSING)
    amount = getattr(item, "amount", _MISSING)
    new_contextid = getattr(item, "new_contextid", _MISSING)
    new_assetid = getattr(item, "new_assetid", _MISSING)
    if type(appid) is not int or appid <= 0:
        raise PlatformAdapterProtocolError("appid must be a positive integer")
    _require_id(contextid, "contextid")
    _require_id(assetid, "assetid")
    if type(amount) is not int or amount <= 0:
        raise PlatformAdapterProtocolError("amount must be a positive integer")
    _require_id(new_contextid, "new_contextid")
    _require_id(new_assetid, "new_assetid")


def _validate_recipient_inventory_item(item: object) -> None:
    appid = getattr(item, "appid", _MISSING)
    contextid = getattr(item, "contextid", _MISSING)
    assetid = getattr(item, "assetid", _MISSING)
    amount = getattr(item, "amount", _MISSING)
    if type(appid) is not int or appid <= 0:
        raise PlatformAdapterProtocolError("appid must be a positive integer")
    _require_id(contextid, "contextid")
    _require_id(assetid, "assetid")
    if type(amount) is not int or amount <= 0:
        raise PlatformAdapterProtocolError("amount must be a positive integer")


def _validate_steam_completed_trade_evidence(evidence: object) -> None:
    steam_tradeoffer_id = getattr(evidence, "steam_tradeoffer_id", _MISSING)
    steam_trade_id = getattr(evidence, "steam_trade_id", _MISSING)
    account_steam_id = getattr(evidence, "account_steam_id", _MISSING)
    counterparty_steam_id = getattr(evidence, "counterparty_steam_id", _MISSING)
    completed_at = getattr(evidence, "completed_at", _MISSING)
    items_given = getattr(evidence, "items_given", _MISSING)
    items_received = getattr(evidence, "items_received", _MISSING)
    inventory_confirmed_items = getattr(
        evidence,
        "inventory_confirmed_items",
        _MISSING,
    )

    _require_id(steam_tradeoffer_id, "steam_tradeoffer_id")
    _require_id(steam_trade_id, "steam_trade_id")
    _require_id(account_steam_id, "account_steam_id")
    _require_id(counterparty_steam_id, "counterparty_steam_id")
    if account_steam_id == counterparty_steam_id:
        raise PlatformAdapterProtocolError(
            "account and counterparty Steam IDs must differ"
        )
    if (
        type(completed_at) not in (int, float)
        or not math.isfinite(completed_at)
        or completed_at < 0
    ):
        raise PlatformAdapterProtocolError(
            "completed_at must be a finite non-negative number"
        )

    for field, items, item_type, validator in (
        (
            "items_given",
            items_given,
            CompletedTradeItemEvidence,
            _validate_completed_trade_item,
        ),
        (
            "items_received",
            items_received,
            CompletedTradeItemEvidence,
            _validate_completed_trade_item,
        ),
        (
            "inventory_confirmed_items",
            inventory_confirmed_items,
            RecipientInventoryItemEvidence,
            _validate_recipient_inventory_item,
        ),
    ):
        if type(items) is not tuple:
            raise PlatformAdapterProtocolError(f"{field} must be a tuple")
        for item in items:
            if type(item) is not item_type:
                raise PlatformAdapterProtocolError(
                    f"{field} must contain {item_type.__name__}"
                )
            validator(item)

    if not items_received:
        raise PlatformAdapterProtocolError("items_received cannot be empty")

    for field, items in (
        ("items_given", items_given),
        ("items_received", items_received),
    ):
        source_identities = [(item.appid, item.contextid, item.assetid) for item in items]
        new_identities = [
            (item.appid, item.new_contextid, item.new_assetid) for item in items
        ]
        if len(set(source_identities)) != len(source_identities):
            raise PlatformAdapterProtocolError(
                f"{field} contains duplicate source identity"
            )
        if len(set(new_identities)) != len(new_identities):
            raise PlatformAdapterProtocolError(
                f"{field} contains duplicate post-trade identity"
            )

    inventory_identities = [
        (item.appid, item.contextid, item.assetid)
        for item in inventory_confirmed_items
    ]
    if len(set(inventory_identities)) != len(inventory_identities):
        raise PlatformAdapterProtocolError(
            "inventory_confirmed_items contains duplicate identity"
        )
    received_identities = {
        (item.appid, item.new_contextid, item.new_assetid, item.amount)
        for item in items_received
    }
    for item in inventory_confirmed_items:
        if (
            item.appid,
            item.contextid,
            item.assetid,
            item.amount,
        ) not in received_identities:
            raise PlatformAdapterProtocolError(
                "inventory confirmation must match received post-trade identity"
            )

    for field, items, key in (
        (
            "items_given",
            items_given,
            lambda item: (
                item.appid,
                item.contextid,
                item.assetid,
                item.new_contextid,
                item.new_assetid,
                item.amount,
            ),
        ),
        (
            "items_received",
            items_received,
            lambda item: (
                item.appid,
                item.contextid,
                item.assetid,
                item.new_contextid,
                item.new_assetid,
                item.amount,
            ),
        ),
        (
            "inventory_confirmed_items",
            inventory_confirmed_items,
            lambda item: (item.appid, item.contextid, item.assetid, item.amount),
        ),
    ):
        object.__setattr__(evidence, field, tuple(sorted(items, key=key)))


_MISSING = object()


def _request_attribute(request: object, field: str) -> object:
    value = getattr(request, field, _MISSING)
    if value is _MISSING:
        raise PlatformAdapterProtocolError(f"request is missing {field}")
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
    if capability in {
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        PlatformCapability.ACCEPT_OFFER,
        PlatformCapability.CONFIRM_OFFER,
    }:
        _require_id(steam_tradeoffer_id, "steam_tradeoffer_id")
    elif steam_tradeoffer_id is not None:
        raise PlatformAdapterProtocolError(
            "steam_tradeoffer_id is only valid for tradeoffer-bound capabilities"
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
            raise PlatformAdapterProtocolError(
                "result request must be a PlatformRequest"
            )
        try:
            PlatformRequest.__post_init__(self.request)
        except PlatformAdapterProtocolError:
            raise
        except Exception as error:
            raise PlatformAdapterProtocolError(
                "result request failed defensive validation"
            ) from error
        if type(self.status) is not PlatformResultStatus:
            raise PlatformAdapterProtocolError(
                "result status must be a PlatformResultStatus"
            )
        if self.detail is not None and (
            type(self.detail) is not str
            or not self.detail
            or self.detail.strip() != self.detail
        ):
            raise PlatformAdapterProtocolError(
                "result detail must be a non-whitespace string"
            )
        if self.status is not PlatformResultStatus.SUCCESS:
            if self.evidence is not None:
                raise PlatformAdapterProtocolError(
                    "non-success results cannot contain evidence"
                )
            return
        expected_evidence = {
            PlatformCapability.READ_DELIVERY_DIRECTION: DeliveryDirectionEvidence,
            PlatformCapability.READ_OFFER_STATE: OfferStateEvidence,
            PlatformCapability.READ_INVENTORY_STATE: InventoryStateEvidence,
            PlatformCapability.READ_STEAM_TRADE_OFFER: SteamTradeOfferEvidence,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE: SteamCompletedTradeEvidence,
            PlatformCapability.SEND_OFFER: SendOfferEvidence,
            PlatformCapability.ACCEPT_OFFER: AcceptOfferEvidence,
            PlatformCapability.CONFIRM_OFFER: ConfirmOfferEvidence,
        }.get(self.request.capability)
        if expected_evidence is None:
            raise PlatformAdapterProtocolError(
                "success is not allowed for this capability"
            )
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
        if self.request.capability in {
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE,
            PlatformCapability.ACCEPT_OFFER,
            PlatformCapability.CONFIRM_OFFER,
        }:
            if (
                self.evidence.steam_tradeoffer_id
                != self.request.steam_tradeoffer_id
                or self.evidence.account_steam_id
                != self.request.recipient_steam_id
            ):
                raise PlatformAdapterProtocolError(
                    "success evidence identity does not match request"
                )

    @property
    def is_success(self) -> bool:
        """Return true only for an explicitly normalized success."""
        return self.status is PlatformResultStatus.SUCCESS


@runtime_checkable
class PlatformAdapter(Protocol):
    """Minimal pure boundary that real adapters may implement."""

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
    """Deterministic local adapter used only to exercise the abstract boundary."""

    def __init__(
        self,
        *,
        capabilities: Iterable[PlatformCapability] = DEFAULT_PLATFORM_CAPABILITIES,
        outcomes: Mapping[PlatformRequest, object] | None = None,
    ) -> None:
        declared = frozenset(capabilities)
        if any(type(item) is not PlatformCapability for item in declared):
            raise PlatformAdapterProtocolError(
                "capabilities must contain PlatformCapability"
            )
        if outcomes is not None and not isinstance(outcomes, Mapping):
            raise PlatformAdapterProtocolError("outcomes must be a mapping")
        configured = {} if outcomes is None else dict(outcomes)
        if any(type(request) is not PlatformRequest for request in configured):
            raise PlatformAdapterProtocolError(
                "outcomes must use PlatformRequest keys"
            )
        self._capabilities = declared
        self._outcomes = MappingProxyType(configured)

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return self._capabilities

    def execute(self, request: PlatformRequest) -> PlatformResult:
        if type(request) is not PlatformRequest:
            raise PlatformAdapterProtocolError(
                "request must be a PlatformRequest"
            )
        PlatformRequest.__post_init__(request)
        if request.capability not in self._capabilities:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="capability_not_declared",
            )

        configured = self._outcomes.get(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
        )
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
                    if configured_status is PlatformResultStatus.SUCCESS
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
    "AcceptOfferEvidence",
    "DEFAULT_PLATFORM_CAPABILITIES",
    "CompletedTradeItemEvidence",
    "ConfirmOfferEvidence",
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
    "RecipientInventoryItemEvidence",
    "SendOfferEvidence",
    "SteamCompletedTradeEvidence",
    "SteamTradeOfferEvidence",
    "SteamTradeOfferLifecycle",
    "TradeOfferItemEvidence",
]
