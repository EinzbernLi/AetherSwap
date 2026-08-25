"""Bounded, fingerprint-gated BUFF read diagnostic for TASK-049.

This module deliberately performs no Store RW open, Store CAS, Steam request,
Host receipt write, normal runtime start, or platform write. It reproduces only
the first recovery tick's BUFF read normalization and emits sanitized schema
stage codes; raw order/account/Steam IDs and credentials are never printed.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Sequence

from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.platform_readonly import BuffReadOnlyAdapter
from app.auto_offer.recovery_command import (
    RecoveryCommandError,
    RecoveryTargetBinding,
    _make_buff_client,
    _strict_hex,
    collect_recovery_preflight,
)

_TIMEOUT_SECONDS = 15.0
_HISTORY_PAGE_SIZE = 10
_ORDER_FIELDS = ("buff_order_id", "bill_order_id")
_RECIPIENT_FIELDS = (
    "recipient_steam_id",
    "recipient_steamid",
    "buyer_steam_id",
    "buyer_steamid",
    "to_steam_id",
    "to_steamid",
)
_TRADE_OFFER_FIELDS = ("tradeofferid", "trade_offer_id")
_SELLER_FIELDS = ("seller_steam_id", "seller_steamid")


@dataclass(frozen=True, slots=True)
class BuffRecoveryDiagnostic:
    current_status: PlatformResultStatus
    current_detail: str
    final_status: PlatformResultStatus
    final_detail: str
    history_fallback_used: bool
    history_schema_trace: tuple[str, ...]


def _request(binding: RecoveryTargetBinding) -> PlatformRequest:
    snapshot = binding.store.snapshot
    return PlatformRequest(
        purchase_id=snapshot.purchase_id,
        buff_order_id=snapshot.buff_order_id,
        account_id=snapshot.account_id,
        recipient_steam_id=snapshot.recipient_steam_id,
        revision=binding.store.revision,
        capability=PlatformCapability.READ_OFFER_STATE,
        timeout_seconds=_TIMEOUT_SECONDS,
    )


def _history_fallback_used(current: PlatformResult) -> bool:
    if current.status in {
        PlatformResultStatus.SUCCESS,
        PlatformResultStatus.MALFORMED,
    }:
        return False
    if (
        current.status is PlatformResultStatus.FAILURE
        and current.detail != "network_failure"
    ):
        return False
    return True


def _canonical_raw_identifier(value: object) -> str | None:
    if type(value) not in (str, int):
        return None
    normalized = str(value)
    if not normalized or normalized.strip() != normalized:
        return None
    return normalized


def _normalized_identity(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _alias_values(
    record: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    canonical: bool,
) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in fields:
        if field not in record:
            continue
        value = (
            _canonical_raw_identifier(record[field])
            if canonical
            else _normalized_identity(record[field])
        )
        if value is None:
            return None
        values.append(value)
    if len(set(values)) > 1:
        return None
    return tuple(values)


def _tradeoffer_alias_invalid_reason(record: Mapping[str, object]) -> str:
    """Classify an invalid Trade Offer alias set without exposing field values."""

    canonical_values: list[str] = []
    saw_null = False
    saw_field = False
    for field in _TRADE_OFFER_FIELDS:
        if field not in record:
            continue
        saw_field = True
        raw = record[field]
        if raw is None:
            saw_null = True
            continue
        canonical = _canonical_raw_identifier(raw)
        if canonical is None:
            if type(raw) is str:
                return "target_tradeoffer_alias_format_invalid"
            return "target_tradeoffer_alias_type_invalid"
        canonical_values.append(canonical)

    if not saw_field:
        return "target_tradeoffer_missing"
    if saw_null:
        return "target_tradeoffer_alias_null"
    if len(set(canonical_values)) > 1:
        return "target_tradeoffer_alias_conflict"
    return "target_tradeoffer_alias_invalid_unclassified"


def _canonical_positive_decimal_text(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        return None
    number = int(value)
    if number <= 0 or str(number) != value:
        return None
    return value


def _classify_history_payload(
    payload: object,
    *,
    expected_page_num: int,
    target_order_id: str,
    recipient_steam_id: str,
) -> str:
    """Classify only schema stages; never include raw field values in output."""

    prefix = f"p{expected_page_num}:"
    if not isinstance(payload, Mapping):
        return prefix + "page_not_mapping"
    if payload.get("code") != "OK":
        return prefix + "code_non_ok"
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return prefix + "data_not_mapping"

    page_num = data.get("page_num")
    if type(page_num) is not int or page_num != expected_page_num:
        return prefix + "page_num_mismatch_or_type"
    page_size = data.get("page_size")
    if type(page_size) is not int or page_size != _HISTORY_PAGE_SIZE:
        return prefix + "page_size_mismatch_or_type"
    total_page = data.get("total_page")
    if type(total_page) is not int or total_page < page_num:
        return prefix + "total_page_invalid"
    items = data.get("items")
    if not isinstance(items, list):
        return prefix + "items_not_list"
    if len(items) > _HISTORY_PAGE_SIZE:
        return prefix + "items_over_page_size"
    if any(not isinstance(item, Mapping) for item in items):
        return prefix + "item_not_mapping"
    if any(_canonical_raw_identifier(item.get("id")) is None for item in items):
        return prefix + "item_id_invalid"

    matches = [
        item
        for item in items
        if _canonical_raw_identifier(item.get("id")) == target_order_id
    ]
    if not matches:
        return prefix + "valid_no_target"
    if len(matches) > 1:
        return prefix + "target_duplicate"
    target = matches[0]

    order_values = _alias_values(target, _ORDER_FIELDS, canonical=True)
    if order_values is None or (order_values and target_order_id not in order_values):
        return prefix + "target_order_alias_invalid"

    recipient_values = _alias_values(target, _RECIPIENT_FIELDS, canonical=False)
    if recipient_values is None:
        return prefix + "target_recipient_alias_invalid"
    if not recipient_values:
        return prefix + "target_recipient_missing"
    if recipient_values[0] != recipient_steam_id:
        return prefix + "target_recipient_mismatch"

    offer_values = _alias_values(target, _TRADE_OFFER_FIELDS, canonical=True)
    if offer_values is None:
        return prefix + _tradeoffer_alias_invalid_reason(target)
    if not offer_values:
        return prefix + "target_tradeoffer_missing"

    seller_values: list[str] = []
    for field in _SELLER_FIELDS:
        if field not in target:
            continue
        value = _canonical_positive_decimal_text(target[field])
        if value is None:
            return prefix + "target_seller_alias_invalid"
        seller_values.append(value)
    if not seller_values:
        return prefix + "target_seller_missing"
    if len(set(seller_values)) != 1:
        return prefix + "target_seller_alias_invalid"
    if seller_values[0] == recipient_steam_id:
        return prefix + "target_seller_equals_recipient"

    return prefix + "target_fields_shape_valid"


class _TracingBuffClient:
    """Delegate exact reads while retaining only sanitized history schema codes."""

    def __init__(self, client: object, binding: RecoveryTargetBinding) -> None:
        self._client = client
        self._target_order_id = binding.store.snapshot.buff_order_id
        self._recipient_steam_id = binding.store.snapshot.recipient_steam_id
        self.history_schema_trace: list[str] = []

    def get_steam_trades(self):
        return self._client.get_steam_trades()

    def get_buy_order_history_page(self, page_num: int, game: str = "csgo"):
        payload = self._client.get_buy_order_history_page(page_num, game)
        self.history_schema_trace.append(
            _classify_history_payload(
                payload,
                expected_page_num=page_num,
                target_order_id=self._target_order_id,
                recipient_steam_id=self._recipient_steam_id,
            )
        )
        return payload


def diagnose_buff_read(binding: RecoveryTargetBinding) -> BuffRecoveryDiagnostic:
    """Run only the BUFF read portion of RESULT_UNKNOWN identity recovery."""

    client = _make_buff_client(binding)
    traced = _TracingBuffClient(client, binding)
    try:
        adapter = BuffReadOnlyAdapter(traced, account_id=binding.account_id)
        request = _request(binding)
        current = adapter.execute(request)
        if type(current) is not PlatformResult:
            raise RecoveryCommandError("buff_diagnostic_result_invalid")

        recover = getattr(adapter, "_recover_result_unknown_offer_state", None)
        if not callable(recover):
            raise RecoveryCommandError("buff_diagnostic_recovery_unavailable")
        fallback_used = _history_fallback_used(current)
        final = recover(request, current)
        if type(final) is not PlatformResult:
            raise RecoveryCommandError("buff_diagnostic_result_invalid")

        return BuffRecoveryDiagnostic(
            current_status=current.status,
            current_detail=current.detail,
            final_status=final.status,
            final_detail=final.detail,
            history_fallback_used=fallback_used,
            history_schema_trace=tuple(traced.history_schema_trace),
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_offer.recovery_diagnostic",
        description="Fingerprint-gated BUFF read-only diagnostic for TASK-049.",
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

        result = diagnose_buff_read(binding)
        trace = ",".join(result.history_schema_trace) or "none"
        print(
            "TASK049_BUFF_DIAGNOSTIC "
            f"current_status={result.current_status.value} "
            f"current_detail={result.current_detail} "
            f"final_status={result.final_status.value} "
            f"final_detail={result.final_detail} "
            f"history_fallback_used={str(result.history_fallback_used).lower()} "
            f"history_schema_trace={trace} "
            "store_rw_opened=false store_cas=0 steam_requests=0 host_writes=0 platform_writes=0"
        )
        return 0
    except RecoveryCommandError as exc:
        print(f"TASK049_BUFF_DIAGNOSTIC_BLOCKED reason={exc}")
        return 2
    except Exception:
        print("TASK049_BUFF_DIAGNOSTIC_BLOCKED reason=unexpected_diagnostic_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
