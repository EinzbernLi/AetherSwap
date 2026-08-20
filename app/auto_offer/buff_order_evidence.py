"""Pure exact BUFF order/item normalization for seller ACCEPT authorization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .adapters import (
    PlatformAdapterProtocolError,
    SellerOrderItemEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)

from .counterparty_evidence import (
    CounterpartyEvidenceError,
    seller_counterparty_from_exact_buff_record,
)


class BuffOrderEvidenceError(ValueError):
    """Exact BUFF evidence is missing, malformed, or ambiguous."""


def _identifier(value: object, *, reason: str) -> str:
    if isinstance(value, bool) or type(value) not in (str, int):
        raise BuffOrderEvidenceError(reason)
    normalized = str(value).strip()
    if not normalized or normalized != str(value):
        raise BuffOrderEvidenceError(reason)
    return normalized


def _positive_goods_id(value: object, *, reason: str) -> int:
    if isinstance(value, bool):
        raise BuffOrderEvidenceError(reason)
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise BuffOrderEvidenceError(reason) from None
    if normalized <= 0 or str(normalized) != str(value):
        raise BuffOrderEvidenceError(reason)
    return normalized


def _one_alias(
    record: Mapping[str, object], fields: tuple[str, ...], *, reason: str
) -> str:
    values = [_identifier(record[field], reason=reason) for field in fields if field in record]
    if not values or len(set(values)) != 1:
        raise BuffOrderEvidenceError(reason)
    return values[0]


ExactSellerBuffItemEvidence = SellerOrderItemEvidence


_ORDER_FIELDS = ("buff_order_id", "bill_order_id")
_RECIPIENT_FIELDS = (
    "recipient_steam_id",
    "recipient_steamid",
    "buyer_steam_id",
    "buyer_steamid",
    "to_steam_id",
    "to_steamid",
)
_OFFER_FIELDS = ("tradeofferid", "trade_offer_id")


def normalize_exact_seller_buff_item(
    records: Sequence[Mapping[str, object]],
    *,
    buff_order_id: str,
    recipient_steam_id: str,
    host_goods_id: int,
) -> ExactSellerBuffItemEvidence:
    """Return evidence only for one exact order, offer, and seller-side item."""

    exact_order = _identifier(buff_order_id, reason="invalid_buff_order_id")
    exact_recipient = _identifier(
        recipient_steam_id, reason="invalid_recipient_steam_id"
    )
    exact_goods_id = _positive_goods_id(host_goods_id, reason="invalid_host_goods_id")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise BuffOrderEvidenceError("invalid_records")
    if any(not isinstance(record, Mapping) for record in records):
        raise BuffOrderEvidenceError("invalid_records")

    matches: list[Mapping[str, object]] = []
    for record in records:
        present = [field for field in _ORDER_FIELDS if field in record]
        if not present:
            continue
        values = [_identifier(record[field], reason="invalid_order_identity") for field in present]
        if len(set(values)) != 1:
            raise BuffOrderEvidenceError("invalid_order_identity")
        if values[0] == exact_order:
            matches.append(record)
    if len(matches) != 1:
        raise BuffOrderEvidenceError("order_mapping_not_unique")

    record = matches[0]
    offer_id = _one_alias(record, _OFFER_FIELDS, reason="tradeoffer_not_proven")
    sharing_offer = 0
    for candidate in records:
        present = [field for field in _OFFER_FIELDS if field in candidate]
        if not present:
            continue
        values = [_identifier(candidate[field], reason="invalid_tradeoffer_id") for field in present]
        if len(set(values)) != 1:
            raise BuffOrderEvidenceError("invalid_tradeoffer_id")
        if values[0] == offer_id:
            sharing_offer += 1
    if sharing_offer != 1:
        raise BuffOrderEvidenceError("aggregated_offer_not_supported")

    recipient = _one_alias(
        record, _RECIPIENT_FIELDS, reason="recipient_steam_id_not_proven"
    )
    if recipient != exact_recipient:
        raise BuffOrderEvidenceError("recipient_steam_id_mismatch")
    try:
        counterparty = seller_counterparty_from_exact_buff_record(record).steam_id
    except CounterpartyEvidenceError as exc:
        raise BuffOrderEvidenceError(str(exc)) from None
    if counterparty == recipient:
        raise BuffOrderEvidenceError("self_counterparty")

    items = record.get("items_to_trade")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
        raise BuffOrderEvidenceError("item_mapping_not_unique")
    item = items[0]
    goods_id = _positive_goods_id(item.get("goods_id"), reason="invalid_goods_id")
    if goods_id != exact_goods_id:
        raise BuffOrderEvidenceError("goods_id_mismatch")
    assetid = _identifier(item.get("assetid"), reason="invalid_seller_assetid")
    try:
        return ExactSellerBuffItemEvidence(
            buff_order_id=exact_order,
            steam_tradeoffer_id=offer_id,
            recipient_steam_id=recipient,
            counterparty_steam_id=counterparty,
            goods_id=goods_id,
            seller_assetid=assetid,
        )
    except PlatformAdapterProtocolError:
        raise BuffOrderEvidenceError("invalid_seller_item_evidence") from None


def authorize_exact_seller_accept(
    buff_evidence: ExactSellerBuffItemEvidence,
    steam_evidence: SteamTradeOfferEvidence,
) -> ExactSellerBuffItemEvidence:
    """Validate the complete exact, single-CS2-item incoming ACCEPT contract."""

    if type(buff_evidence) is not ExactSellerBuffItemEvidence:
        raise BuffOrderEvidenceError("invalid_buff_evidence")
    if type(steam_evidence) is not SteamTradeOfferEvidence:
        raise BuffOrderEvidenceError("invalid_steam_evidence")
    if steam_evidence.steam_tradeoffer_id != buff_evidence.steam_tradeoffer_id:
        raise BuffOrderEvidenceError("tradeoffer_id_mismatch")
    if steam_evidence.account_steam_id != buff_evidence.recipient_steam_id:
        raise BuffOrderEvidenceError("recipient_steam_id_mismatch")
    if steam_evidence.counterparty_steam_id != buff_evidence.counterparty_steam_id:
        raise BuffOrderEvidenceError("seller_steam_id_mismatch")
    if steam_evidence.is_our_offer:
        raise BuffOrderEvidenceError("incoming_offer_required")
    if steam_evidence.lifecycle is not SteamTradeOfferLifecycle.ACTIVE:
        raise BuffOrderEvidenceError("active_offer_required")
    if steam_evidence.items_to_give != ():
        raise BuffOrderEvidenceError("outgoing_items_present")
    if len(steam_evidence.items_to_receive) != 1:
        raise BuffOrderEvidenceError("steam_item_mapping_not_unique")
    item = steam_evidence.items_to_receive[0]
    if item.appid != 730 or item.amount != 1:
        raise BuffOrderEvidenceError("cs2_item_identity_mismatch")
    if item.assetid != buff_evidence.seller_assetid:
        raise BuffOrderEvidenceError("seller_assetid_mismatch")
    return buff_evidence


__all__ = [
    "BuffOrderEvidenceError",
    "ExactSellerBuffItemEvidence",
    "authorize_exact_seller_accept",
    "normalize_exact_seller_buff_item",
]
