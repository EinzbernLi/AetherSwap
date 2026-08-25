"""Fingerprint-gated exact BUFF history target field-shape diagnostic for TASK-049.

This command performs only bounded BUFF buy-order-history GETs. It never opens the
Auto Offer Store read-write, performs Store CAS, accesses Steam, invokes a Host
writer, starts normal runtime, or exposes any platform write capability. Output
is restricted to whitelisted field shapes and counts; raw identities, asset IDs,
URLs, tokens, cookies, and credentials are never printed.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

from app.auto_offer.recovery_command import (
    RecoveryCommandError,
    RecoveryTargetBinding,
    _make_buff_client,
    _strict_hex,
    collect_recovery_preflight,
)

_MAX_HISTORY_PAGES = 3
_HISTORY_PAGE_SIZE = 10
_SELLER_FIELDS = ("seller_steam_id", "seller_steamid")


@dataclass(frozen=True, slots=True)
class BuffTargetFieldDiagnostic:
    target_page: int
    history_requests: int
    seller_trace: str
    items_trace: str
    trade_offer_url_trace: str


def _canonical_identifier(value: object) -> str | None:
    if isinstance(value, bool) or type(value) not in (str, int):
        return None
    normalized = str(value)
    if not normalized or normalized.strip() != normalized:
        return None
    return normalized


def _positive_decimal_scalar(value: object) -> str | None:
    if isinstance(value, bool) or type(value) not in (str, int):
        return None
    normalized = str(value)
    if (
        not normalized
        or normalized.strip() != normalized
        or not normalized.isascii()
        or not normalized.isdecimal()
        or normalized.startswith("0")
    ):
        return None
    number = int(normalized)
    if number <= 0 or str(number) != normalized:
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


def _seller_shape(record: Mapping[str, object]) -> str:
    shapes: dict[str, str] = {}
    canonical: list[str] = []
    present_count = 0
    valid = True
    for field in _SELLER_FIELDS:
        present = field in record
        value = record.get(field)
        shapes[field] = _value_shape(present=present, value=value)
        if present:
            present_count += 1
            normalized = _positive_decimal_scalar(value)
            if normalized is None:
                valid = False
            else:
                canonical.append(normalized)

    if present_count == 0:
        relation = "none"
    elif not valid:
        relation = "invalid"
    elif len(set(canonical)) != 1:
        relation = "conflict"
    elif present_count == 1:
        relation = "single"
    else:
        relation = "equal"

    return (
        f"seller_steam_id={shapes['seller_steam_id']}|"
        f"seller_steamid={shapes['seller_steamid']}|"
        f"relation={relation}"
    )


def _items_shape(record: Mapping[str, object]) -> str:
    if "items_to_trade" not in record:
        return "items_to_trade=absent"
    items = record.get("items_to_trade")
    if items is None:
        return "items_to_trade=null"
    if not isinstance(items, list):
        return "items_to_trade=" + _value_shape(present=True, value=items)

    prefix = f"items_to_trade=list_count_{len(items)}"
    if len(items) != 1:
        return prefix
    item = items[0]
    if not isinstance(item, Mapping):
        return prefix + "|item0=" + _value_shape(present=True, value=item)

    goods_present = "goods_id" in item
    asset_present = "assetid" in item
    goods_value = item.get("goods_id")
    asset_value = item.get("assetid")
    goods_shape = _value_shape(present=goods_present, value=goods_value)
    asset_shape = _value_shape(present=asset_present, value=asset_value)
    goods_relation = (
        "positive_decimal" if goods_present and _positive_decimal_scalar(goods_value) is not None else "not_proven"
    )
    asset_relation = (
        "canonical" if asset_present and _canonical_identifier(asset_value) is not None else "not_proven"
    )
    return (
        prefix
        + "|item0=mapping"
        + f"|goods_id={goods_shape}|goods_id_relation={goods_relation}"
        + f"|assetid={asset_shape}|assetid_relation={asset_relation}"
    )


def _trade_offer_url_kind(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        return "not_parseable"
    try:
        parsed = urlsplit(value)
    except Exception:
        return "not_parseable"
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        return "not_parseable"
    host = (parsed.hostname or "").casefold()
    if host not in {"steamcommunity.com", "www.steamcommunity.com"}:
        return "non_steam"
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() == "tradeoffer":
        second = parts[1].casefold()
        if second == "new":
            return "steam_new_offer_link"
        if second.isascii() and second.isdecimal() and not second.startswith("0"):
            number = int(second)
            if number > 0 and str(number) == second:
                return "steam_view_offer_with_id"
    return "steam_other"


def _trade_offer_url_shape(record: Mapping[str, object]) -> str:
    present = "trade_offer_url" in record
    value = record.get("trade_offer_url")
    shape = _value_shape(present=present, value=value)
    kind = "absent" if not present else _trade_offer_url_kind(value)
    return f"trade_offer_url={shape}|kind={kind}"


def _page_target(
    payload: object,
    *,
    expected_page_num: int,
    target_order_id: str,
) -> tuple[Mapping[str, object] | None, int]:
    if not isinstance(payload, Mapping) or payload.get("code") != "OK":
        raise RecoveryCommandError("history_page_invalid")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RecoveryCommandError("history_page_invalid")
    page_num = data.get("page_num")
    page_size = data.get("page_size")
    total_page = data.get("total_page")
    items = data.get("items")
    if (
        type(page_num) is not int
        or page_num != expected_page_num
        or type(page_size) is not int
        or page_size != _HISTORY_PAGE_SIZE
        or type(total_page) is not int
        or total_page < page_num
        or not isinstance(items, list)
        or len(items) > _HISTORY_PAGE_SIZE
        or any(not isinstance(item, Mapping) for item in items)
    ):
        raise RecoveryCommandError("history_page_invalid")

    matches: list[Mapping[str, object]] = []
    for item in items:
        item_id = _canonical_identifier(item.get("id"))
        if item_id is None:
            raise RecoveryCommandError("history_item_id_invalid")
        if item_id == target_order_id:
            matches.append(item)
    if len(matches) > 1:
        raise RecoveryCommandError("history_target_duplicate")
    return (matches[0] if matches else None), total_page


def diagnose_target_fields(binding: RecoveryTargetBinding) -> BuffTargetFieldDiagnostic:
    client = _make_buff_client(binding)
    history_requests = 0
    try:
        target_order_id = binding.store.snapshot.buff_order_id
        total_page = _MAX_HISTORY_PAGES
        for page_num in range(1, _MAX_HISTORY_PAGES + 1):
            if page_num > total_page:
                break
            payload = client.get_buy_order_history_page(page_num, "csgo")
            history_requests += 1
            target, total_page = _page_target(
                payload,
                expected_page_num=page_num,
                target_order_id=target_order_id,
            )
            if target is None:
                continue
            return BuffTargetFieldDiagnostic(
                target_page=page_num,
                history_requests=history_requests,
                seller_trace=_seller_shape(target),
                items_trace=_items_shape(target),
                trade_offer_url_trace=_trade_offer_url_shape(target),
            )
        raise RecoveryCommandError("history_target_not_found")
    finally:
        try:
            client.close()
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_offer.recovery_target_diagnostic",
        description="Fingerprint-gated exact BUFF history field-shape diagnostic for TASK-049.",
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-fingerprint", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected_fingerprint = _strict_hex(
            args.expected_fingerprint,
            64,
            "expected_fingerprint_invalid",
        )
        binding = collect_recovery_preflight(
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
        )
        if binding.fingerprint != expected_fingerprint:
            raise RecoveryCommandError("target_fingerprint_mismatch")

        result = diagnose_target_fields(binding)
        print(
            "TASK049_BUFF_TARGET_FIELD_DIAGNOSTIC "
            f"target_page={result.target_page} "
            f"history_requests={result.history_requests} "
            f"seller_trace={result.seller_trace} "
            f"items_trace={result.items_trace} "
            f"trade_offer_url_trace={result.trade_offer_url_trace} "
            "store_rw_opened=false store_cas=0 steam_requests=0 host_writes=0 platform_writes=0"
        )
        return 0
    except RecoveryCommandError as exc:
        print(f"TASK049_BUFF_TARGET_FIELD_DIAGNOSTIC_BLOCKED reason={exc}")
        return 2
    except Exception:
        print("TASK049_BUFF_TARGET_FIELD_DIAGNOSTIC_BLOCKED reason=unexpected_diagnostic_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
