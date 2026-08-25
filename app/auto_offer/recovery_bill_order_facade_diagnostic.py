"""Superseding TASK-049 exact BUFF bill-order diagnostic via BuffClient ownership.

The first bill-order diagnostic correctly failed closed because BuffClient does
not expose BuffBuyer._make_request.  This command preserves BuffClient's auth
lock, credential snapshot and cookie-persistence policy by entering the owned
buyer only through BuffClient._run(), then executes one fixed GET for the exact
persisted bill order.  No generic request surface is exported to normal runtime.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence

from app.auto_offer.recovery_bill_order_diagnostic import (
    BuffBillOrderDiagnostic,
    _API_BILL_ORDER_BATCH_INFO,
    _SELLER_FIELDS,
    _TRADE_FIELDS,
    _alias_trace,
    _asset_info_trace,
    _candidate_records,
    _items_trace,
    _url_trace,
)
from app.auto_offer.recovery_command import (
    RecoveryCommandError,
    RecoveryTargetBinding,
    _make_buff_client,
    _strict_hex,
    collect_recovery_preflight,
)


def _read_exact_bill_order_info(client: object, target_order_id: str) -> object:
    """Perform exactly one fixed read while retaining BuffClient ownership."""

    runner = getattr(client, "_run", None)
    if not callable(runner):
        raise RecoveryCommandError("buff_facade_read_runner_unavailable")

    def operation(buyer: object) -> object:
        request = getattr(buyer, "_make_request", None)
        if not callable(request):
            raise RecoveryCommandError("buff_buyer_read_method_unavailable")
        return request(
            "GET",
            _API_BILL_ORDER_BATCH_INFO,
            params={"bill_orders": target_order_id},
            headers={
                "Referer": "https://buff.163.com/market/buy_order/history?game=csgo"
            },
        )

    return runner(operation)


def diagnose_bill_order(binding: RecoveryTargetBinding) -> BuffBillOrderDiagnostic:
    client = _make_buff_client(binding)
    try:
        target_order_id = binding.store.snapshot.buff_order_id
        payload = _read_exact_bill_order_info(client, target_order_id)
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
        prog="python -m app.auto_offer.recovery_bill_order_facade_diagnostic",
        description="Fingerprint-gated exact BUFF bill-order diagnostic via BuffClient ownership.",
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
            "TASK049_BUFF_BILL_ORDER_FACADE_DIAGNOSTIC "
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
        print(f"TASK049_BUFF_BILL_ORDER_FACADE_DIAGNOSTIC_BLOCKED reason={exc}")
        return 2
    except Exception:
        print(
            "TASK049_BUFF_BILL_ORDER_FACADE_DIAGNOSTIC_BLOCKED "
            "reason=unexpected_diagnostic_error"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
