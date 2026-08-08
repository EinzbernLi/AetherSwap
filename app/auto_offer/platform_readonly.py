"""Fail-closed read-only adapters for the native Auto Offer boundary.

The adapters wrap already-owned platform readers supplied by dependency
injection.  They do not create sessions, perform authentication, persist
state, retry, or execute platform mutations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from app.auto_offer.adapters import (
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformAdapter,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)


BUFF_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
    }
)
STEAM_INVENTORY_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.READ_INVENTORY_STATE}
)
STEAM_TRADE_OFFER_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.READ_STEAM_TRADE_OFFER}
)

_ORDER_FIELDS: Final[tuple[str, ...]] = ("buff_order_id", "bill_order_id")
_RECIPIENT_FIELDS: Final[tuple[str, ...]] = (
    "recipient_steam_id",
    "recipient_steamid",
    "buyer_steam_id",
    "buyer_steamid",
    "to_steam_id",
    "to_steamid",
)
_SELLER_FIELDS: Final[tuple[str, ...]] = (
    "seller_steam_id",
    "seller_steamid",
)
_DIRECTION_FIELDS: Final[tuple[str, ...]] = (
    "direction",
    "delivery_direction",
    "offer_direction",
)
_PENDING_STATES: Final[frozenset[object]] = frozenset(
    {
        1,
        "1",
        "pending",
        "awaiting_offer",
        "offer_pending",
        "waiting",
        "waiting_for_offer",
        "to_receive",
    }
)


@runtime_checkable
class BuffReadOnlyClient(Protocol):
    """The one read-only method required from the existing BUFF client."""

    def get_steam_trades(self) -> object:
        ...


@runtime_checkable
class SteamInventoryReader(Protocol):
    """A host-provided reader bound to one explicit Steam identity."""

    def __call__(self, steam_id: str) -> object:
        ...


@runtime_checkable
class SteamTradeOfferReader(Protocol):
    """A host-provided reader for one exact Steam Trade Offer ID."""

    def __call__(self, steam_tradeoffer_id: str) -> object:
        ...


def _result(
    request: PlatformRequest,
    status: PlatformResultStatus,
    detail: str,
    evidence: DeliveryDirectionEvidence
    | OfferStateEvidence
    | InventoryStateEvidence
    | SteamTradeOfferEvidence
    | None = None,
) -> PlatformResult:
    return PlatformResult(
        request=request,
        status=status,
        detail=detail,
        evidence=evidence,
    )


def _request_or_raise(request: object) -> PlatformRequest:
    if type(request) is not PlatformRequest:
        raise PlatformAdapterProtocolError("request must be a PlatformRequest")
    PlatformRequest.__post_init__(request)
    return request


def _normalize_identifier(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")
    return value


def _is_auth_error(error: BaseException) -> bool:
    name = type(error).__name__.lower()
    return any(token in name for token in ("auth", "unauthor", "forbidden"))


@dataclass(frozen=True)
class _ReadFailure:
    """A private exception mapping that cannot be forged as a result."""

    status: PlatformResultStatus
    detail: str


def _call_read(operation: Callable[[], object]) -> object | _ReadFailure:
    try:
        return operation()
    except (PlatformAdapterTimeoutError, TimeoutError):
        return _ReadFailure(PlatformResultStatus.TIMEOUT, "timeout")
    except Exception as error:
        detail = "auth_failed" if _is_auth_error(error) else "network_failure"
        return _ReadFailure(PlatformResultStatus.FAILURE, detail)


def _records_from_payload(payload: object) -> list[Mapping[str, Any]] | PlatformResultStatus:
    if payload is None:
        return PlatformResultStatus.RESULT_UNKNOWN
    if not isinstance(payload, list):
        return PlatformResultStatus.MALFORMED
    if any(not isinstance(record, Mapping) for record in payload):
        return PlatformResultStatus.MALFORMED
    return payload


def _canonical_order_values(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in _ORDER_FIELDS:
        if field not in record:
            continue
        value = _normalize_identifier(record[field])
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _exact_order_matches(
    records: list[Mapping[str, Any]], order_id: str
) -> list[Mapping[str, Any]] | None:
    matches: list[Mapping[str, Any]] = []
    for record in records:
        values = _canonical_order_values(record)
        if values is None:
            return None
        if order_id in values:
            matches.append(record)
    return matches


def _identity_values(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in fields:
        if field not in record:
            continue
        value = _normalize_identifier(record[field])
        if value is None:
            return None
        values.append(value)
    if len(set(values)) > 1:
        return None
    return tuple(values)


def _recipient_binding_failure(
    record: Mapping[str, Any], recipient: str
) -> tuple[PlatformResultStatus, str] | None:
    recipient_values = _identity_values(record, _RECIPIENT_FIELDS)
    if recipient_values is None:
        return (PlatformResultStatus.MALFORMED, "malformed_payload")
    if not recipient_values:
        return (PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven")
    if recipient_values[0] != recipient:
        return (PlatformResultStatus.FAILURE, "identity_mismatch")
    return None


def _proves_seller_direction(record: Mapping[str, Any]) -> bool | None:

    direction_values = _identity_values(record, _DIRECTION_FIELDS)
    if direction_values is None:
        return None
    if direction_values and direction_values[0] in {
        "seller_sends_offer",
        "seller_to_buyer",
        "seller-to-buyer",
    }:
        return True
    if direction_values and direction_values[0] == "buyer_sends_offer":
        return False

    seller_values = _identity_values(record, _SELLER_FIELDS)
    if seller_values is None:
        return None
    return bool(seller_values)


def _trade_offer_id(record: Mapping[str, Any]) -> str | None:
    for field in ("tradeofferid", "trade_offer_id"):
        if field in record:
            value = _normalize_identifier(record[field])
            if value is None:
                return None
            return value
    return None


class BuffReadOnlyAdapter:
    """Map one injected BUFF trade reader into the normalized result contract."""

    def __init__(self, client: BuffReadOnlyClient, *, account_id: str) -> None:
        if not callable(getattr(client, "get_steam_trades", None)):
            raise PlatformAdapterProtocolError(
                "client must provide get_steam_trades"
            )
        self._client = client
        self._account_id = _require_identifier(account_id, "account_id")

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return BUFF_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        request = _request_or_raise(request)
        if request.capability not in self.capabilities:
            return _result(
                request, PlatformResultStatus.UNSUPPORTED, "unsupported_capability"
            )
        if request.account_id != self._account_id:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        raw = _call_read(self._client.get_steam_trades)
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        parsed = _records_from_payload(raw)
        if isinstance(parsed, PlatformResultStatus):
            if parsed is PlatformResultStatus.RESULT_UNKNOWN:
                return _result(request, parsed, "order_not_proven")
            return _result(request, parsed, "malformed_payload")

        matches = _exact_order_matches(parsed, request.buff_order_id)
        if matches is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if not matches:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        if len(matches) > 1:
            return _result(
                request, PlatformResultStatus.MALFORMED, "ambiguous_order"
            )

        record = matches[0]
        recipient_failure = _recipient_binding_failure(
            record, request.recipient_steam_id
        )
        if recipient_failure is not None:
            return _result(request, *recipient_failure)
        if request.capability is PlatformCapability.READ_DELIVERY_DIRECTION:
            proven = _proves_seller_direction(record)
            if proven is None:
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if not proven:
                return _result(
                    request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
                )
            return _result(
                request,
                PlatformResultStatus.SUCCESS,
                "seller_sends_offer",
                DeliveryDirectionEvidence(),
            )

        offer_id = _trade_offer_id(record)
        if offer_id is None:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        state = record.get("state")
        if state not in _PENDING_STATES:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "offer_pending",
            OfferStateEvidence(offer_id),
        )


def _inventory_evidence(payload: Mapping[str, Any]) -> InventoryStateEvidence | None:
    """Extract only canonical recipient-side asset IDs from a valid envelope."""

    has_assets = "assets" in payload
    assets = payload.get("assets")
    has_count = "total_inventory_count" in payload
    total_count = payload.get("total_inventory_count")

    if has_assets and not isinstance(assets, list):
        return None
    if has_count and (type(total_count) is not int or total_count < 0):
        return None
    if not has_assets:
        if not has_count or total_count != 0:
            return None
        return InventoryStateEvidence(assetids=(), total_inventory_count=0)

    assetids: list[str] = []
    for asset in assets:
        if not isinstance(asset, Mapping) or "assetid" not in asset:
            return None
        assetid = asset["assetid"]
        if type(assetid) is not str or not assetid or assetid.strip() != assetid:
            return None
        assetids.append(assetid)
    if total_count is None:
        total_count = 0 if not assetids else None
    try:
        return InventoryStateEvidence(
            assetids=tuple(assetids), total_inventory_count=total_count
        )
    except PlatformAdapterProtocolError:
        return None


class SteamInventoryReadOnlyAdapter:
    """Read one explicitly bound Steam inventory through injected code."""

    def __init__(
        self,
        reader: SteamInventoryReader,
        *,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        if not callable(reader):
            raise PlatformAdapterProtocolError("reader must be callable")
        self._reader = reader
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_identifier(
            recipient_steam_id, "recipient_steam_id"
        )

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return STEAM_INVENTORY_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        request = _request_or_raise(request)
        if request.capability not in self.capabilities:
            return _result(
                request, PlatformResultStatus.UNSUPPORTED, "unsupported_capability"
            )
        if (
            request.account_id != self._account_id
            or request.recipient_steam_id != self._recipient_steam_id
        ):
            return _result(
                request, PlatformResultStatus.FAILURE, "identity_mismatch"
            )

        raw = _call_read(lambda: self._reader(self._recipient_steam_id))
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        if isinstance(raw, Mapping) and raw.get("auth_expired") is True:
            return _result(request, PlatformResultStatus.FAILURE, "auth_failed")
        if raw is None:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        if not isinstance(raw, Mapping):
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if "success" not in raw:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if raw.get("success") != 1:
            return _result(request, PlatformResultStatus.FAILURE, "network_failure")
        evidence = _inventory_evidence(raw)
        if evidence is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "inventory_snapshot_readable",
            evidence,
        )


def _steam_trade_offer_items(
    payload: Mapping[str, Any], field: str
) -> tuple[TradeOfferItemEvidence, ...] | None:
    raw_items = payload.get(field)
    if type(raw_items) is not list:
        return None
    items: list[TradeOfferItemEvidence] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            return None
        required = ("appid", "contextid", "assetid", "amount")
        if any(key not in raw_item for key in required):
            return None
        try:
            items.append(
                TradeOfferItemEvidence(
                    appid=raw_item["appid"],
                    contextid=raw_item["contextid"],
                    assetid=raw_item["assetid"],
                    amount=raw_item["amount"],
                )
            )
        except PlatformAdapterProtocolError:
            return None
    return tuple(items)


def _steam_trade_offer_evidence(
    request: PlatformRequest, payload: Mapping[str, Any]
) -> PlatformResult:
    required = (
        "steam_tradeoffer_id",
        "account_steam_id",
        "counterparty_steam_id",
        "is_our_offer",
        "lifecycle",
        "items_to_give",
        "items_to_receive",
    )
    if any(key not in payload for key in required):
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )

    tradeoffer_id = payload["steam_tradeoffer_id"]
    account_steam_id = payload["account_steam_id"]
    counterparty_steam_id = payload["counterparty_steam_id"]
    if type(tradeoffer_id) is not str or tradeoffer_id != request.steam_tradeoffer_id:
        return _result(
            request, PlatformResultStatus.FAILURE, "identity_mismatch"
        )
    if type(account_steam_id) is not str or account_steam_id != request.recipient_steam_id:
        return _result(
            request, PlatformResultStatus.FAILURE, "identity_mismatch"
        )
    if type(counterparty_steam_id) is not str:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    if type(payload["is_our_offer"]) is not bool:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    if (
        not counterparty_steam_id
        or counterparty_steam_id.strip() != counterparty_steam_id
        or counterparty_steam_id == account_steam_id
    ):
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )

    items_to_give = _steam_trade_offer_items(payload, "items_to_give")
    items_to_receive = _steam_trade_offer_items(payload, "items_to_receive")
    if items_to_give is None or items_to_receive is None:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    for items in (items_to_give, items_to_receive):
        identities = [
            (item.appid, item.contextid, item.assetid) for item in items
        ]
        if len(set(identities)) != len(identities):
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
    if not items_to_give and not items_to_receive:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )

    lifecycle = payload["lifecycle"]
    if type(lifecycle) is not str:
        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "trade_offer_state_not_proven",
        )
    lifecycle_value = {
        "active": SteamTradeOfferLifecycle.ACTIVE,
        "accepted": SteamTradeOfferLifecycle.ACCEPTED,
    }.get(lifecycle)
    if lifecycle_value is None:
        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "trade_offer_state_not_proven",
        )

    try:
        evidence = SteamTradeOfferEvidence(
            steam_tradeoffer_id=tradeoffer_id,
            account_steam_id=account_steam_id,
            counterparty_steam_id=counterparty_steam_id,
            is_our_offer=payload["is_our_offer"],
            lifecycle=lifecycle_value,
            items_to_give=items_to_give,
            items_to_receive=items_to_receive,
        )
    except PlatformAdapterProtocolError:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    detail = (
        "trade_offer_active"
        if lifecycle_value is SteamTradeOfferLifecycle.ACTIVE
        else "trade_offer_accepted"
    )
    return _result(
        request, PlatformResultStatus.SUCCESS, detail, evidence
    )


class SteamTradeOfferReadOnlyAdapter:
    """Map one injected exact-ID Steam Trade Offer reader into evidence."""

    def __init__(
        self,
        reader: SteamTradeOfferReader,
        *,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        if not callable(reader):
            raise PlatformAdapterProtocolError("reader must be callable")
        self._reader = reader
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_identifier(
            recipient_steam_id, "recipient_steam_id"
        )

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return STEAM_TRADE_OFFER_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        request = _request_or_raise(request)
        if request.capability not in self.capabilities:
            return _result(
                request, PlatformResultStatus.UNSUPPORTED, "unsupported_capability"
            )
        if request.account_id != self._account_id:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")
        if request.recipient_steam_id != self._recipient_steam_id:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        raw = _call_read(
            lambda: self._reader(request.steam_tradeoffer_id)
        )
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        if raw is None:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "trade_offer_not_proven",
            )
        if not isinstance(raw, Mapping):
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        return _steam_trade_offer_evidence(request, raw)


__all__ = [
    "BUFF_CAPABILITIES",
    "BuffReadOnlyAdapter",
    "BuffReadOnlyClient",
    "STEAM_INVENTORY_CAPABILITIES",
    "SteamInventoryReader",
    "SteamInventoryReadOnlyAdapter",
    "STEAM_TRADE_OFFER_CAPABILITIES",
    "SteamTradeOfferReader",
    "SteamTradeOfferReadOnlyAdapter",
]
