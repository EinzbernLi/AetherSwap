"""Fail-closed read-only adapters for the native Auto Offer boundary.

The adapters wrap already-owned platform readers supplied by dependency
injection.  They do not create sessions, perform authentication, persist
state, retry, or execute platform mutations.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from app.auto_offer.adapters import (
    CompletedTradeItemEvidence,
    BuffOrderLifecycle,
    BuffOrderLifecycleEvidence,
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
    RecipientInventoryItemEvidence,
    SellerOrderItemEvidence,
    SteamCompletedTradeEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.buff_order_evidence import (
    BuffOrderEvidenceError,
    normalize_exact_seller_buff_item,
)
from app.auto_offer.counterparty_evidence import (
    CounterpartyEvidenceError,
    seller_counterparty_from_exact_buff_record,
)
from app.auto_offer.steam_lifecycle import (
    SteamLifecycleEvidenceError,
    map_exact_steam_lifecycle,
)


BUFF_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE,
        PlatformCapability.READ_SELLER_OFFER_ITEM,
        PlatformCapability.READ_BUFF_ORDER_LIFECYCLE,
    }
)
STEAM_INVENTORY_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.READ_INVENTORY_STATE}
)
STEAM_TRADE_OFFER_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.READ_STEAM_TRADE_OFFER}
)
STEAM_COMPLETED_TRADE_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.READ_STEAM_COMPLETED_TRADE}
)

# Existing seller/direction semantics retain the legacy explicit aliases.  The
# buyer realtime identity bridge additionally accepts BUFF's canonical ``id``
# field without broadening seller-side authority.
_ORDER_FIELDS: Final[tuple[str, ...]] = ("buff_order_id", "bill_order_id")
_REALTIME_ORDER_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "buff_order_id",
    "bill_order_id",
)
_TRADE_OFFER_FIELDS: Final[tuple[str, ...]] = (
    "tradeofferid",
    "trade_offer_id",
)
_HISTORICAL_TRADE_OFFER_URL_PATHS: Final[frozenset[str]] = frozenset(
    {"trade", "tradeoffer"}
)
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
_HISTORICAL_COUNTERPARTY_FIELDS: Final[tuple[str, ...]] = (
    "seller_steam_id",
    "seller_steamid",
    "counterparty_steam_id",
    "counterparty_steamid",
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
_MAX_BUFF_LIFECYCLE_PAGES: Final[int] = 3
_BUFF_HISTORY_PAGE_SIZE: Final[int] = 10
_REFUNDED_TIMEOUT_FIELDS: Final[tuple[str, ...]] = (
    "pay_expire_timeout",
    "deliver_expire_timeout",
    "receive_expire_timeout",
    "buyer_send_offer_timeout",
)


@runtime_checkable
class BuffReadOnlyClient(Protocol):
    """Realtime read-only methods supplied by the existing BUFF client."""

    def get_steam_trades(self) -> object:
        ...


@runtime_checkable
class BuffHistoricalOrderReadOnlyClient(Protocol):
    """Optional historical BUFF order reader used only by recovery paths."""

    def get_buy_order_history_page(self, page_num: int, game: str = "csgo") -> object:
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


@runtime_checkable
class SteamCompletedTradeReader(Protocol):
    """A host-provided reader for one exact completed trade and recipient."""

    def __call__(
        self, steam_tradeoffer_id: str, recipient_steam_id: str
    ) -> object:
        ...


def _result(
    request: PlatformRequest,
    status: PlatformResultStatus,
    detail: str,
    evidence: DeliveryDirectionEvidence
    | OfferStateEvidence
    | SellerOrderItemEvidence
    | InventoryStateEvidence
    | SteamTradeOfferEvidence
    | SteamCompletedTradeEvidence
    | BuffOrderLifecycleEvidence
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


def _canonical_raw_identifier(value: object) -> str | None:
    if type(value) not in (str, int):
        return None
    normalized = str(value)
    if not normalized or normalized.strip() != normalized:
        return None
    return normalized


def _canonical_positive_decimal_text(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        return None
    number = int(value)
    if number <= 0 or str(number) != value:
        return None
    return value


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")
    return value


@dataclass(frozen=True)
class _RecoveryAccountLineage:
    """Immutable account IDs admitted only by an explicit recovery surface."""

    current_account_id: str
    accepted_account_ids: frozenset[str]

    def __post_init__(self) -> None:
        current = _require_identifier(self.current_account_id, "account_id")
        if type(self.accepted_account_ids) is not frozenset:
            raise PlatformAdapterProtocolError(
                "recovery account lineage must be immutable"
            )
        if not self.accepted_account_ids:
            raise PlatformAdapterProtocolError(
                "recovery account lineage must not be empty"
            )
        for account_id in self.accepted_account_ids:
            _require_identifier(account_id, "account_id")
        if current not in self.accepted_account_ids:
            raise PlatformAdapterProtocolError(
                "recovery account lineage must include current account"
            )


def _make_recovery_account_lineage(
    current_account_id: str,
    persisted_account_ids: frozenset[str],
) -> _RecoveryAccountLineage:
    """Create the narrow immutable allowlist used by recovery-only wiring."""

    current = _require_identifier(current_account_id, "account_id")
    if type(persisted_account_ids) is not frozenset:
        raise PlatformAdapterProtocolError(
            "persisted account lineage must be immutable"
        )
    accepted = frozenset({current, *persisted_account_ids})
    return _RecoveryAccountLineage(current, accepted)


def _accepted_account_ids_for(
    account_id: str,
    recovery_lineage: _RecoveryAccountLineage | None,
) -> frozenset[str]:
    if recovery_lineage is None:
        return frozenset({account_id})
    if (
        type(recovery_lineage) is not _RecoveryAccountLineage
        or recovery_lineage.current_account_id != account_id
    ):
        raise PlatformAdapterProtocolError(
            "recovery account lineage does not match current account"
        )
    return recovery_lineage.accepted_account_ids


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


def _call_lifecycle_read(operation: Callable[[], object]) -> object | _ReadFailure:
    try:
        return operation()
    except (PlatformAdapterTimeoutError, TimeoutError):
        return _ReadFailure(PlatformResultStatus.TIMEOUT, "timeout")
    except Exception as error:
        name = type(error).__name__.casefold()
        if _is_auth_error(error):
            detail = "auth_failed"
        elif "verification" in name or "captcha" in name:
            detail = "verification_required"
        elif "rate" in name and "limit" in name:
            detail = "rate_limited"
        elif "risk" in name:
            detail = "risk_control"
        else:
            detail = "network_failure"
        return _ReadFailure(PlatformResultStatus.FAILURE, detail)


@dataclass(frozen=True)
class _BuffHistoryPage:
    page_num: int
    total_page: int
    items: tuple[Mapping[str, Any], ...]


def _parse_buff_history_page(
    payload: object,
    *,
    expected_page_num: int,
) -> _BuffHistoryPage | _ReadFailure:
    if not isinstance(payload, Mapping):
        return _ReadFailure(PlatformResultStatus.MALFORMED, "malformed_payload")
    if payload.get("code") != "OK":
        return _ReadFailure(PlatformResultStatus.RESULT_UNKNOWN, "history_non_ok")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return _ReadFailure(PlatformResultStatus.MALFORMED, "malformed_payload")
    page_num = data.get("page_num")
    page_size = data.get("page_size")
    total_page = data.get("total_page")
    items = data.get("items")
    if (
        type(page_num) is not int
        or page_num != expected_page_num
        or type(page_size) is not int
        or page_size != _BUFF_HISTORY_PAGE_SIZE
        or type(total_page) is not int
        or total_page < page_num
        or not isinstance(items, list)
        or len(items) > _BUFF_HISTORY_PAGE_SIZE
        or any(not isinstance(item, Mapping) for item in items)
    ):
        return _ReadFailure(PlatformResultStatus.MALFORMED, "malformed_payload")
    for item in items:
        if _canonical_raw_identifier(item.get("id")) is None:
            return _ReadFailure(PlatformResultStatus.MALFORMED, "malformed_payload")
    return _BuffHistoryPage(page_num, total_page, tuple(items))


def _finite_number(value: object) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _classify_exact_buff_history_item(
    item: Mapping[str, Any],
    *,
    buff_order_id: str,
    page_num: int,
) -> BuffOrderLifecycleEvidence | _ReadFailure:
    if _canonical_raw_identifier(item.get("id")) != buff_order_id:
        return _ReadFailure(PlatformResultStatus.MALFORMED, "identity_mismatch")
    state = item.get("state")
    state_text = item.get("state_text")
    if type(state) is not str or type(state_text) is not str:
        return _ReadFailure(PlatformResultStatus.MALFORMED, "malformed_payload")

    if state == "PAYING" and state_text == "等待付款":
        expires = _finite_number(item.get("pay_expire_timeout"))
        if expires is None or expires <= 0:
            return _ReadFailure(
                PlatformResultStatus.RESULT_UNKNOWN,
                "order_state_unproven",
            )
        return BuffOrderLifecycleEvidence(
            buff_order_id=buff_order_id,
            lifecycle=BuffOrderLifecycle.PAYING,
            raw_state=state,
            raw_state_text=state_text,
            page_num=page_num,
        )

    if state == "FAIL" and state_text == "购买失败-已退款":
        for field in _REFUNDED_TIMEOUT_FIELDS:
            value = _finite_number(item.get(field))
            if value is None or value != -1:
                return _ReadFailure(
                    PlatformResultStatus.RESULT_UNKNOWN,
                    "order_state_unproven",
                )
        if (
            "tradeofferid" not in item
            or "trade_offer_url" not in item
            or item.get("tradeofferid") is not None
            or item.get("trade_offer_url") is not None
        ):
            return _ReadFailure(
                PlatformResultStatus.RESULT_UNKNOWN,
                "order_state_unproven",
            )
        return BuffOrderLifecycleEvidence(
            buff_order_id=buff_order_id,
            lifecycle=BuffOrderLifecycle.REFUNDED,
            raw_state=state,
            raw_state_text=state_text,
            page_num=page_num,
        )

    return _ReadFailure(
        PlatformResultStatus.RESULT_UNKNOWN,
        "order_state_unproven",
    )


def _records_from_payload(payload: object) -> list[Mapping[str, Any]] | PlatformResultStatus:
    if payload is None:
        return PlatformResultStatus.RESULT_UNKNOWN
    if not isinstance(payload, list):
        return PlatformResultStatus.MALFORMED
    if any(not isinstance(record, Mapping) for record in payload):
        return PlatformResultStatus.MALFORMED
    return payload


def _canonical_alias_values(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in fields:
        if field not in record:
            continue
        value = _canonical_raw_identifier(record[field])
        if value is None:
            return None
        values.append(value)
    if len(set(values)) > 1:
        return None
    return tuple(values)


def _canonical_order_values(
    record: Mapping[str, Any],
    fields: tuple[str, ...] = _ORDER_FIELDS,
) -> tuple[str, ...] | None:
    return _canonical_alias_values(record, fields)


def _exact_order_matches(
    records: list[Mapping[str, Any]],
    order_id: str,
    *,
    fields: tuple[str, ...] = _ORDER_FIELDS,
) -> list[Mapping[str, Any]] | None:
    matches: list[Mapping[str, Any]] = []
    for record in records:
        values = _canonical_order_values(record, fields)
        if values is None:
            return None
        if order_id in values:
            matches.append(record)
    return matches


def _exact_wait_send_order_matches(
    records: list[Mapping[str, Any]], order_id: str
) -> list[Mapping[str, Any]] | None:
    matches: list[Mapping[str, Any]] = []
    for record in records:
        value = record.get("id")
        if isinstance(value, bool) or type(value) not in (str, int):
            return None
        normalized = str(value)
        if not normalized or normalized.strip() != normalized:
            return None
        if normalized == order_id:
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


def _optional_recipient_binding_failure(
    record: Mapping[str, Any], recipient: str
) -> tuple[PlatformResultStatus, str] | None:
    """Validate an explicit BUFF recipient without requiring the field to exist."""

    recipient_values = _identity_values(record, _RECIPIENT_FIELDS)
    if recipient_values is None:
        return (PlatformResultStatus.MALFORMED, "malformed_payload")
    if recipient_values and recipient_values[0] != recipient:
        return (PlatformResultStatus.FAILURE, "identity_mismatch")
    return None


def _historical_trade_offer_url_is_valid(
    value: object,
    *,
    expected_offer_id: str,
) -> bool:
    """Validate an optional Steam URL without treating it as identity proof."""

    if type(value) is not str or not value or value.strip() != value:
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or hostname is None
        or hostname.casefold() != "steamcommunity.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if not segments or segments[0].casefold() not in _HISTORICAL_TRADE_OFFER_URL_PATHS:
        return False
    if len(segments) > 1 and segments[1].casefold() != "new":
        embedded = _canonical_raw_identifier(segments[1])
        if embedded is None or embedded != expected_offer_id:
            return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    for field in ("tradeofferid", "trade_offer_id"):
        values = query.get(field, [])
        if len(values) > 1 or (values and values[0] != expected_offer_id):
            return False
    if (
        query.get("tradeofferid")
        and query.get("trade_offer_id")
        and query["tradeofferid"] != query["trade_offer_id"]
    ):
        return False
    return True


def _historical_trade_offer_values(
    record: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Read optional historical offer aliases, treating empty values as absent."""

    values: list[str] = []
    for field in _TRADE_OFFER_FIELDS:
        if field not in record or record[field] in (None, ""):
            continue
        value = _canonical_raw_identifier(record[field])
        if value is None:
            return None
        values.append(value)
    if len(set(values)) > 1:
        return None
    return tuple(values)


def _historical_counterparty_values(
    record: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Read optional seller/counterparty aliases in canonical Steam form."""

    values: list[str] = []
    for field in _HISTORICAL_COUNTERPARTY_FIELDS:
        if field not in record:
            continue
        value = _canonical_positive_decimal_text(record[field])
        if value is None:
            return None
        values.append(value)
    if len(set(values)) > 1:
        return None
    return tuple(values)


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


class BuffReadOnlyAdapter:
    """Map one injected BUFF trade reader into the normalized result contract."""

    def __init__(
        self,
        client: BuffReadOnlyClient,
        *,
        account_id: str,
        historical_client: BuffHistoricalOrderReadOnlyClient | None = None,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        if not callable(getattr(client, "get_steam_trades", None)):
            raise PlatformAdapterProtocolError(
                "client must provide get_steam_trades"
            )
        self._client = client
        if historical_client is None:
            history_reader = getattr(client, "get_buy_order_history_page", None)
            if callable(history_reader):
                historical_client = cast(BuffHistoricalOrderReadOnlyClient, client)
        elif not callable(
            getattr(historical_client, "get_buy_order_history_page", None)
        ):
            raise PlatformAdapterProtocolError(
                "historical client must provide get_buy_order_history_page"
            )
        self._historical_client = historical_client
        self._account_id = _require_identifier(account_id, "account_id")
        self._accepted_account_ids = _accepted_account_ids_for(
            self._account_id,
            recovery_lineage,
        )

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return BUFF_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        request = _request_or_raise(request)
        if request.capability not in self.capabilities:
            return _result(
                request, PlatformResultStatus.UNSUPPORTED, "unsupported_capability"
            )
        if request.account_id not in self._accepted_account_ids:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        if request.capability is PlatformCapability.READ_BUFF_ORDER_LIFECYCLE:
            return self._execute_order_lifecycle(request)
        if request.capability is PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE:
            return self._execute_historical_buyer_offer_state(request)

        raw = _call_read(self._client.get_steam_trades)
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        parsed = _records_from_payload(raw)
        if isinstance(parsed, PlatformResultStatus):
            if parsed is PlatformResultStatus.RESULT_UNKNOWN:
                if request.capability is PlatformCapability.READ_DELIVERY_DIRECTION:
                    return self._execute_buyer_wait_send(request)
                return _result(request, parsed, "order_not_proven")
            return _result(request, parsed, "malformed_payload")

        if request.capability is PlatformCapability.READ_SELLER_OFFER_ITEM:
            return self._execute_seller_offer_item(request, parsed)

        match_fields = (
            _REALTIME_ORDER_FIELDS
            if request.capability is PlatformCapability.READ_OFFER_STATE
            else _ORDER_FIELDS
        )
        matches = _exact_order_matches(
            parsed,
            request.buff_order_id,
            fields=match_fields,
        )
        if matches is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if not matches:
            if request.capability is PlatformCapability.READ_DELIVERY_DIRECTION:
                return self._execute_buyer_wait_send(request)
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        if len(matches) > 1:
            return _result(
                request, PlatformResultStatus.MALFORMED, "ambiguous_order"
            )

        record = matches[0]
        if request.capability is PlatformCapability.READ_DELIVERY_DIRECTION:
            recipient_failure = _recipient_binding_failure(
                record, request.recipient_steam_id
            )
            if recipient_failure is not None:
                return _result(request, *recipient_failure)
            proven = _proves_seller_direction(record)
            if proven is None:
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if not proven:
                return self._execute_buyer_wait_send(request)
            try:
                counterparty = seller_counterparty_from_exact_buff_record(
                    record
                ).steam_id
            except CounterpartyEvidenceError:
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if counterparty == request.recipient_steam_id:
                return _result(
                    request, PlatformResultStatus.FAILURE, "identity_mismatch"
                )
            return _result(
                request,
                PlatformResultStatus.SUCCESS,
                "seller_sends_offer",
                DeliveryDirectionEvidence(
                    "seller_sends_offer", counterparty
                ),
            )

        # Legacy synthetic/explicit alias records retain their original strict
        # seller+recipient+pending semantics.  The canonical realtime ``id``
        # record is the narrow buyer recovery bridge and delegates
        # counterparty/direction/items/lifecycle authority to exact Steam read.
        if "id" not in record:
            recipient_failure = _recipient_binding_failure(
                record, request.recipient_steam_id
            )
            if recipient_failure is not None:
                return _result(request, *recipient_failure)
            offer_values = _canonical_alias_values(record, _TRADE_OFFER_FIELDS)
            if offer_values is None:
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if not offer_values:
                return _result(
                    request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
                )
            try:
                counterparty = seller_counterparty_from_exact_buff_record(record).steam_id
            except CounterpartyEvidenceError as exc:
                if str(exc) == "seller_steam_id_not_proven":
                    return _result(
                        request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
                    )
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if counterparty == request.recipient_steam_id:
                return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")
            if record.get("state") not in _PENDING_STATES:
                return _result(
                    request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
                )
            return _result(
                request,
                PlatformResultStatus.SUCCESS,
                "offer_pending",
                OfferStateEvidence(offer_values[0], counterparty),
            )

        recipient_failure = _optional_recipient_binding_failure(
            record, request.recipient_steam_id
        )
        if recipient_failure is not None:
            return _result(request, *recipient_failure)
        offer_values = _canonical_alias_values(record, _TRADE_OFFER_FIELDS)
        if offer_values is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if not offer_values:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        offer_id = offer_values[0]

        seller_values = _identity_values(record, _SELLER_FIELDS)
        if seller_values is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        counterparty: str | None = None
        if seller_values:
            try:
                counterparty = seller_counterparty_from_exact_buff_record(record).steam_id
            except CounterpartyEvidenceError:
                return _result(
                    request, PlatformResultStatus.MALFORMED, "malformed_payload"
                )
            if counterparty == request.recipient_steam_id:
                return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "offer_bound_realtime",
            OfferStateEvidence(offer_id, counterparty),
        )

    def _execute_seller_offer_item(
        self,
        request: PlatformRequest,
        records: list[Mapping[str, Any]],
    ) -> PlatformResult:
        matches = _exact_order_matches(records, request.buff_order_id)
        if matches is None:
            return _result(
                request,
                PlatformResultStatus.MALFORMED,
                "malformed_payload",
            )
        if not matches:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "order_not_proven",
            )
        if len(matches) > 1:
            return _result(
                request,
                PlatformResultStatus.MALFORMED,
                "ambiguous_order",
            )
        try:
            evidence = normalize_exact_seller_buff_item(
                records,
                buff_order_id=request.buff_order_id,
                recipient_steam_id=request.recipient_steam_id,
                host_goods_id=request.host_goods_id,
            )
        except BuffOrderEvidenceError:
            return _result(
                request,
                PlatformResultStatus.MALFORMED,
                "seller_item_not_proven",
            )
        if (
            evidence.buff_order_id != request.buff_order_id
            or evidence.steam_tradeoffer_id != request.steam_tradeoffer_id
            or evidence.recipient_steam_id != request.recipient_steam_id
            or evidence.counterparty_steam_id != request.counterparty_steam_id
            or evidence.goods_id != request.host_goods_id
        ):
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "identity_mismatch",
            )
        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "seller_offer_item_proven",
            evidence,
        )

    def _execute_buyer_wait_send(self, request: PlatformRequest) -> PlatformResult:
        reader = getattr(
            self._client,
            "get_buy_orders_waiting_to_" + "send_" + "offer",
            None,
        )
        if not callable(reader):
            return _result(request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven")

        raw = _call_read(lambda: reader("csgo", 730))
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        parsed = _records_from_payload(raw)
        if isinstance(parsed, PlatformResultStatus):
            if parsed is PlatformResultStatus.RESULT_UNKNOWN:
                return _result(request, parsed, "order_not_proven")
            return _result(request, parsed, "malformed_payload")

        matches = _exact_wait_send_order_matches(parsed, request.buff_order_id)
        if matches is None:
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        if not matches:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        if len(matches) > 1:
            return _result(request, PlatformResultStatus.MALFORMED, "ambiguous_order")

        record = matches[0]
        if "buyer_steamid" not in record:
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        buyer_steam_id = _canonical_positive_decimal_text(record["buyer_steamid"])
        if buyer_steam_id is None:
            return _result(request, PlatformResultStatus.MALFORMED, "malformed_payload")
        if buyer_steam_id != request.recipient_steam_id:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")
        if record.get("state_text") != "等待你发起报价":
            return _result(
                request, PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"
            )
        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "buyer_sends_offer",
            DeliveryDirectionEvidence("buyer_sends_offer"),
        )

    def _execute_order_lifecycle(self, request: PlatformRequest) -> PlatformResult:
        reader = self._history_reader()
        if not callable(reader):
            return _result(
                request,
                PlatformResultStatus.UNSUPPORTED,
                "history_reader_not_available",
            )

        for page_num in range(1, _MAX_BUFF_LIFECYCLE_PAGES + 1):
            raw = _call_lifecycle_read(
                lambda page_num=page_num: reader(page_num, "csgo")
            )
            if isinstance(raw, _ReadFailure):
                return _result(request, raw.status, raw.detail)
            parsed = _parse_buff_history_page(
                raw,
                expected_page_num=page_num,
            )
            if isinstance(parsed, _ReadFailure):
                return _result(request, parsed.status, parsed.detail)

            matches = [
                item
                for item in parsed.items
                if _canonical_raw_identifier(item.get("id"))
                == request.buff_order_id
            ]
            if len(matches) > 1:
                return _result(
                    request,
                    PlatformResultStatus.MALFORMED,
                    "ambiguous_order",
                )
            if len(matches) == 1:
                classified = _classify_exact_buff_history_item(
                    matches[0],
                    buff_order_id=request.buff_order_id,
                    page_num=page_num,
                )
                if isinstance(classified, _ReadFailure):
                    return _result(request, classified.status, classified.detail)
                return _result(
                    request,
                    PlatformResultStatus.SUCCESS,
                    classified.lifecycle.value,
                    classified,
                )
            if page_num >= parsed.total_page:
                break

        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "order_not_proven",
        )

    def _execute_historical_buyer_offer_state(
        self,
        request: PlatformRequest,
    ) -> PlatformResult:
        reader = self._history_reader()
        if not callable(reader):
            return _result(
                request,
                PlatformResultStatus.UNSUPPORTED,
                "history_reader_not_available",
            )

        matches: list[Mapping[str, Any]] = []
        first_total_page: int | None = None
        scan_target = _MAX_BUFF_LIFECYCLE_PAGES
        for page_num in range(1, _MAX_BUFF_LIFECYCLE_PAGES + 1):
            raw = _call_lifecycle_read(
                lambda page_num=page_num: reader(page_num, "csgo")
            )
            if isinstance(raw, _ReadFailure):
                return _result(request, raw.status, raw.detail)
            parsed = _parse_buff_history_page(
                raw,
                expected_page_num=page_num,
            )
            if isinstance(parsed, _ReadFailure):
                return _result(request, parsed.status, parsed.detail)
            if first_total_page is None:
                first_total_page = parsed.total_page
                scan_target = min(first_total_page, _MAX_BUFF_LIFECYCLE_PAGES)
            elif parsed.total_page != first_total_page:
                return _result(
                    request,
                    PlatformResultStatus.MALFORMED,
                    "malformed_payload",
                )

            page_matches = [
                item
                for item in parsed.items
                if _canonical_raw_identifier(item.get("id"))
                == request.buff_order_id
            ]
            if len(matches) + len(page_matches) > 1:
                return _result(
                    request,
                    PlatformResultStatus.MALFORMED,
                    "ambiguous_order",
                )
            matches.extend(page_matches)
            if page_num == scan_target:
                break

        if len(matches) == 1:
            return self._historical_offer_evidence(request, matches[0])
        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "order_not_proven",
        )

    def _history_reader(self) -> Callable[[int, str], object] | None:
        if self._historical_client is None:
            return None
        reader = getattr(self._historical_client, "get_buy_order_history_page", None)
        return reader if callable(reader) else None

    def _historical_offer_evidence(
        self,
        request: PlatformRequest,
        record: Mapping[str, Any],
    ) -> PlatformResult:
        recipient_failure = _optional_recipient_binding_failure(
            record, request.recipient_steam_id
        )
        if recipient_failure is not None:
            return _result(request, *recipient_failure)
        offer_values = _historical_trade_offer_values(record)
        if offer_values is None:
            return _result(
                request,
                PlatformResultStatus.MALFORMED,
                "malformed_payload",
            )
        if not offer_values:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "order_not_proven",
            )
        offer_id = offer_values[0]
        if "trade_offer_url" in record and record["trade_offer_url"] is not None:
            if not _historical_trade_offer_url_is_valid(
                record["trade_offer_url"],
                expected_offer_id=offer_id,
            ):
                return _result(
                    request,
                    PlatformResultStatus.MALFORMED,
                    "malformed_payload",
                )

        counterparty_values = _historical_counterparty_values(record)
        if counterparty_values is None:
            return _result(
                request,
                PlatformResultStatus.MALFORMED,
                "malformed_payload",
            )
        counterparty: str | None = None
        if counterparty_values:
            counterparty = _canonical_positive_decimal_text(counterparty_values[0])
            if counterparty is None:
                return _result(
                    request,
                    PlatformResultStatus.MALFORMED,
                    "malformed_payload",
                )
            if counterparty == request.recipient_steam_id:
                return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "offer_bound_historical",
            OfferStateEvidence(offer_id, counterparty),
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
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        if not callable(reader):
            raise PlatformAdapterProtocolError("reader must be callable")
        self._reader = reader
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_identifier(
            recipient_steam_id, "recipient_steam_id"
        )
        self._accepted_account_ids = _accepted_account_ids_for(
            self._account_id,
            recovery_lineage,
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
            request.account_id not in self._accepted_account_ids
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
    try:
        lifecycle_evidence = map_exact_steam_lifecycle(lifecycle)
    except SteamLifecycleEvidenceError:
        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "trade_offer_state_not_proven",
        )
    lifecycle_value = lifecycle_evidence.lifecycle

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
    return _result(
        request, PlatformResultStatus.SUCCESS, lifecycle_evidence.detail, evidence
    )


class SteamTradeOfferReadOnlyAdapter:
    """Map one injected exact-ID Steam Trade Offer reader into evidence."""

    def __init__(
        self,
        reader: SteamTradeOfferReader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        if not callable(reader):
            raise PlatformAdapterProtocolError("reader must be callable")
        self._reader = reader
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_identifier(
            recipient_steam_id, "recipient_steam_id"
        )
        self._accepted_account_ids = _accepted_account_ids_for(
            self._account_id,
            recovery_lineage,
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
        if request.account_id not in self._accepted_account_ids:
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


def _completed_trade_items(
    payload: Mapping[str, Any], field: str
) -> tuple[CompletedTradeItemEvidence, ...] | None:
    raw_items = payload.get(field)
    if type(raw_items) is not list:
        return None
    items: list[CompletedTradeItemEvidence] = []
    required = (
        "appid",
        "contextid",
        "assetid",
        "amount",
        "new_contextid",
        "new_assetid",
    )
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            return None
        if any(key not in raw_item for key in required):
            return None
        try:
            items.append(
                CompletedTradeItemEvidence(
                    appid=raw_item["appid"],
                    contextid=raw_item["contextid"],
                    assetid=raw_item["assetid"],
                    amount=raw_item["amount"],
                    new_contextid=raw_item["new_contextid"],
                    new_assetid=raw_item["new_assetid"],
                )
            )
        except PlatformAdapterProtocolError:
            return None
    return tuple(items)


def _recipient_inventory_items(
    payload: Mapping[str, Any], field: str
) -> tuple[RecipientInventoryItemEvidence, ...] | None:
    raw_items = payload.get(field)
    if type(raw_items) is not list:
        return None
    items: list[RecipientInventoryItemEvidence] = []
    required = ("appid", "contextid", "assetid", "amount")
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            return None
        if any(key not in raw_item for key in required):
            return None
        try:
            items.append(
                RecipientInventoryItemEvidence(
                    appid=raw_item["appid"],
                    contextid=raw_item["contextid"],
                    assetid=raw_item["assetid"],
                    amount=raw_item["amount"],
                )
            )
        except PlatformAdapterProtocolError:
            return None
    return tuple(items)


def _steam_completed_trade_evidence(
    request: PlatformRequest, payload: Mapping[str, Any]
) -> PlatformResult:
    required = (
        "steam_tradeoffer_id",
        "steam_trade_id",
        "account_steam_id",
        "counterparty_steam_id",
        "completed_at",
        "items_given",
        "items_received",
        "inventory_confirmed_items",
    )
    if any(key not in payload for key in required):
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )

    tradeoffer_id = payload["steam_tradeoffer_id"]
    account_steam_id = payload["account_steam_id"]
    if type(tradeoffer_id) is not str or tradeoffer_id != request.steam_tradeoffer_id:
        return _result(
            request, PlatformResultStatus.FAILURE, "identity_mismatch"
        )
    if (
        type(account_steam_id) is not str
        or account_steam_id != request.recipient_steam_id
    ):
        return _result(
            request, PlatformResultStatus.FAILURE, "identity_mismatch"
        )

    items_given = _completed_trade_items(payload, "items_given")
    items_received = _completed_trade_items(payload, "items_received")
    inventory_confirmed_items = _recipient_inventory_items(
        payload, "inventory_confirmed_items"
    )
    if (
        items_given is None
        or items_received is None
        or inventory_confirmed_items is None
    ):
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    try:
        evidence = SteamCompletedTradeEvidence(
            steam_tradeoffer_id=tradeoffer_id,
            steam_trade_id=payload["steam_trade_id"],
            account_steam_id=account_steam_id,
            counterparty_steam_id=payload["counterparty_steam_id"],
            completed_at=payload["completed_at"],
            items_given=items_given,
            items_received=items_received,
            inventory_confirmed_items=inventory_confirmed_items,
        )
    except PlatformAdapterProtocolError:
        return _result(
            request, PlatformResultStatus.MALFORMED, "malformed_payload"
        )
    return _result(
        request,
        PlatformResultStatus.SUCCESS,
        "completed_trade_proven",
        evidence,
    )


class SteamCompletedTradeReadOnlyAdapter:
    """Read one exact completed trade through an injected normalized reader."""

    def __init__(
        self,
        reader: SteamCompletedTradeReader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        if not callable(reader):
            raise PlatformAdapterProtocolError("reader must be callable")
        self._reader = reader
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_identifier(
            recipient_steam_id, "recipient_steam_id"
        )
        self._accepted_account_ids = _accepted_account_ids_for(
            self._account_id,
            recovery_lineage,
        )

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return STEAM_COMPLETED_TRADE_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        request = _request_or_raise(request)
        if request.capability not in self.capabilities:
            return _result(
                request, PlatformResultStatus.UNSUPPORTED, "unsupported_capability"
            )
        if request.account_id not in self._accepted_account_ids:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")
        if request.recipient_steam_id != self._recipient_steam_id:
            return _result(request, PlatformResultStatus.FAILURE, "identity_mismatch")

        raw = _call_read(
            lambda: self._reader(
                request.steam_tradeoffer_id, request.recipient_steam_id
            )
        )
        if isinstance(raw, _ReadFailure):
            return _result(request, raw.status, raw.detail)
        if raw is None:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "completed_trade_not_proven",
            )
        if not isinstance(raw, Mapping):
            return _result(
                request, PlatformResultStatus.MALFORMED, "malformed_payload"
            )
        return _steam_completed_trade_evidence(request, raw)


__all__ = [
    "BUFF_CAPABILITIES",
    "BuffHistoricalOrderReadOnlyClient",
    "BuffReadOnlyAdapter",
    "BuffReadOnlyClient",
    "STEAM_INVENTORY_CAPABILITIES",
    "STEAM_COMPLETED_TRADE_CAPABILITIES",
    "SteamInventoryReader",
    "SteamInventoryReadOnlyAdapter",
    "SteamCompletedTradeReader",
    "SteamCompletedTradeReadOnlyAdapter",
    "STEAM_TRADE_OFFER_CAPABILITIES",
    "SteamTradeOfferReader",
    "SteamTradeOfferReadOnlyAdapter",
]
