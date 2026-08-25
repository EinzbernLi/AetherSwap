"""Fingerprint-gated exact BUFF bill-order-info read diagnostic for TASK-049.

This command performs exactly one authenticated BUFF GET for the already-persisted
bill order ID and emits only sanitized field-shape evidence. It never opens the
Auto Offer Store read-write, performs Store CAS, accesses Steam, invokes a Host
writer, starts normal runtime, or exposes platform write capability.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.auto_offer.recovery_command import (
    RecoveryCommandError,
    RecoveryTargetBinding,
    _make_buff_client,
    _strict_hex,
    collect_recovery_preflight,
)

_API_BILL_ORDER_BATCH_INFO = "https://buff.163.com/api/market/bill_order/batch/info"
_ORDER_FIELDS = ("id", "buff_order_id", "bill_order_id")
_SELLER_FIELDS = ("seller_steam_id", "seller_steamid")
_TRADE_FIELDS = ("tradeofferid", "trade_offer_id")


@dataclass(frozen=True, slots=True)
class BuffBillOrderDiagnostic:
    binding_trace: str
    trade_trace: str
    seller_trace: str
    items_trace: str
    asset_info_trace: str
    trade_offer_url_trace: str
    request_count: int = 1


def _canonical_identifier(value: object) -> str | None:
    if isinstance(value, bool) or type(value) not in (str, int):
        return None
    normalized = str(value)
    if not normalized or normalized.strip() != normalized:
        return None
    return normalized


def _value_shape(*, present: bool, value: object) -> str:
    if not present:
        return "absent"
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int_canonical" if _canonical_identifier(value) is not None else "int_noncanonical"
    if type(value) is str:
        return "string_canonical" if _canonical_identifier(value) is not None else "string_noncanonical"
    if type(value) is float:
        return "float"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, list):
        return "list"
    return "other"


def _alias_trace(record: Mapping[str, object], fields: tuple[str, ...], prefix: str) -> str:
    shapes: list[str] = []
    canonical: list[str] = []
    present = 0
    invalid = False
    for field in fields:
        is_present = field in record
        value = record.get(field)
        shapes.append(f"{field}={_value_shape(present=is_present, value=value)}")
        if is_present:
            present += 1
            normalized = _canonical_identifier(value)
            if normalized is None:
                invalid = True
            else:
                canonical.append(normalized)
    if present == 0:
        relation = "none"
    elif invalid:
        relation = "invalid"
    elif len(set(canonical)) != 1:
        relation = "conflict"
    elif present == 1:
        relation = "single"
    else:
        relation = "equal"
    return prefix + ":" + "|".join(shapes) + f"|relation={relation}"


def _items_trace(record: Mapping[str, object]) -> str:
    if "items_to_trade" not in record:
        return "items_to_trade=absent"
    value = record.get("items_to_trade")
    if value is None:
        return "items_to_trade=null"
    if not isinstance(value, list):
        return "items_to_trade=" + _value_shape(present=True, value=value)
    trace = f"items_to_trade=list_count_{len(value)}"
    if len(value) != 1:
        return trace
    item = value[0]
    if not isinstance(item, Mapping):
        return trace + "|item0=" + _value_shape(present=True, value=item)
    parts = [trace, "item0=mapping"]
    for field in ("goods_id", "assetid", "classid", "instanceid"):
        parts.append(
            f"{field}={_value_shape(present=field in item, value=item.get(field))}"
        )
    return "|".join(parts)


def _asset_info_trace(record: Mapping[str, object]) -> str:
    if "asset_info" not in record:
        return "asset_info=absent"
    value = record.get("asset_info")
    if value is None:
        return "asset_info=null"
    if not isinstance(value, Mapping):
        return "asset_info=" + _value_shape(present=True, value=value)
    parts = ["asset_info=mapping"]
    for field in ("assetid", "goods_id", "classid", "instanceid"):
        parts.append(
            f"{field}={_value_shape(present=field in value, value=value.get(field))}"
        )
    return "|".join(parts)


def _url_trace(record: Mapping[str, object]) -> str:
    present = "trade_offer_url" in record
    value = record.get("trade_offer_url")
    shape = _value_shape(present=present, value=value)
    if not present:
        kind = "absent"
    elif value is None:
        kind = "null"
    elif type(value) is not str or not value or value.strip() != value:
        kind = "invalid"
    elif "steamcommunity.com/tradeoffer/" in value.casefold():
        kind = "steam_tradeoffer_url"
    else:
        kind = "other_string"
    return f"trade_offer_url={shape}|kind={kind}"


def _record_order_values(record: Mapping[str, object]) -> set[str] | None:
    values: set[str] = set()
    for field in _ORDER_FIELDS:
        if field not in record:
            continue
        normalized = _canonical_identifier(record[field])
        if normalized is None:
            return None
        values.add(normalized)
    return values


def _candidate_records(data: object, target_order_id: str) -> tuple[list[Mapping[str, object]], str]:
    candidates: list[Mapping[str, object]] = []
    binding_kinds: list[str] = []

    def consider(record: object, *, kind: str) -> None:
        if not isinstance(record, Mapping):
            return
        values = _record_order_values(record)
        if values is None:
            raise RecoveryCommandError("bill_order_identity_malformed")
        if target_order_id in values:
            candidates.append(record)
            binding_kinds.append(kind)

    if isinstance(data, Mapping):
        keyed = data.get(target_order_id)
        if isinstance(keyed, Mapping):
            candidates.append(keyed)
            binding_kinds.append("data_key")
        consider(data, kind="data_record")
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                consider(item, kind="data_items")
        orders = data.get("orders")
        if isinstance(orders, list):
            for item in orders:
                consider(item, kind="data_orders")
    elif isinstance(data, list):
        for item in data:
            consider(item, kind="data_list")
    else:
        raise RecoveryCommandError("bill_order_data_shape_invalid")

    unique: list[Mapping[str, object]] = []
    unique_kinds: list[str] = []
    seen_ids: set[int] = set()
    for record, kind in zip(candidates, binding_kinds, strict=True):
        marker = id(record)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        unique.append(record)
        unique_kinds.append(kind)
    if not unique:
        raise RecoveryCommandError("bill_order_target_not_proven")
    if len(unique) != 1:
        raise RecoveryCommandError("bill_order_target_ambiguous")
    return unique, unique_kinds[0]


def diagnose_bill_order(binding: RecoveryTargetBinding) -> BuffBillOrderDiagnostic:
    client = _make_buff_client(binding)
    try:
        request = getattr(client, "_make_request", None)
        if not callable(request):
            raise RecoveryCommandError("buff_read_method_unavailable")
        target_order_id = binding.store.snapshot.buff_order_id
        payload = request(
            "GET",
            _API_BILL_ORDER_BATCH_INFO,
            params={"bill_orders": target_order_id},
            headers={
                "Referer": "https://buff.163.com/market/buy_order/history?game=csgo"
            },
        )
        if not isinstance(payload, Mapping):
            raise RecoveryCommandError("bill_order_payload_invalid")
        if payload.get("code") != "OK":
            raise RecoveryCommandError("bill_order_non_ok")
        if "data" not in payload:
            raise RecoveryCommandError("bill_order_data_missing")
        records, binding_kind = _candidate_records(payload.get("data"), target_order_id)
        record = records[0]
        return BuffBillOrderDiagnostic(
            binding_trace=f"exact_target={binding_kind}",
            trade_trace=_alias_trace(record, _TRADE_FIELDS, "trade"),
            seller_trace=_alias_trace(record, _SELLER_FIELDS, "seller"),
            items_trace=_items_trace(record),
            asset_info_trace=_asset_info_trace(record),
            trade_offer_url_trace=_url_trace(record),
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_offer.recovery_bill_order_diagnostic",
        description="Fingerprint-gated exact BUFF bill-order-info diagnostic for TASK-049.",
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-fingerprint", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected_fingerprint = _strict_hex(
            args.expected_fingerprint, 64, "expected_fingerprint_invalid"
        )
        binding = collect_recovery_preflight(
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
        )
        if binding.fingerprint != expected_fingerprint:
            raise RecoveryCommandError("target_fingerprint_mismatch")
        result = diagnose_bill_order(binding)
        print(
            "TASK049_BUFF_BILL_ORDER_DIAGNOSTIC "
            f"binding_trace={result.binding_trace} "
            f"trade_trace={result.trade_trace} "
            f"seller_trace={result.seller_trace} "
            f"items_trace={result.items_trace} "
            f"asset_info_trace={result.asset_info_trace} "
            f"trade_offer_url_trace={result.trade_offer_url_trace} "
            f"buff_requests={result.request_count} "
            "store_rw_opened=false store_cas=0 steam_requests=0 host_writes=0 platform_writes=0"
        )
        return 0
    except RecoveryCommandError as exc:
        print(f"TASK049_BUFF_BILL_ORDER_DIAGNOSTIC_BLOCKED reason={exc}")
        return 2
    except Exception:
        print("TASK049_BUFF_BILL_ORDER_DIAGNOSTIC_BLOCKED reason=unexpected_diagnostic_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
