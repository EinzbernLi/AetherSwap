"""Bounded, fingerprint-gated BUFF read diagnostic for TASK-049.

This module deliberately performs no Store RW open, Store CAS, Steam request,
Host receipt write, normal runtime start, or platform write.  It reproduces only
the first recovery tick's BUFF read normalization so a safe-stop can be
classified without exposing raw identities or credentials.
"""

from __future__ import annotations

import argparse
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


@dataclass(frozen=True, slots=True)
class BuffRecoveryDiagnostic:
    current_status: PlatformResultStatus
    current_detail: str
    final_status: PlatformResultStatus
    final_detail: str
    history_fallback_used: bool


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


def diagnose_buff_read(binding: RecoveryTargetBinding) -> BuffRecoveryDiagnostic:
    """Run only the BUFF read portion of RESULT_UNKNOWN identity recovery."""

    client = _make_buff_client(binding)
    try:
        adapter = BuffReadOnlyAdapter(client, account_id=binding.account_id)
        request = _request(binding)
        current = adapter.execute(request)
        if type(current) is not PlatformResult:
            raise RecoveryCommandError("buff_diagnostic_result_invalid")

        recover = getattr(adapter, "_recover_result_unknown_offer_state", None)
        if not callable(recover):
            raise RecoveryCommandError("buff_diagnostic_recovery_unavailable")
        final = recover(request, current)
        if type(final) is not PlatformResult:
            raise RecoveryCommandError("buff_diagnostic_result_invalid")

        return BuffRecoveryDiagnostic(
            current_status=current.status,
            current_detail=current.detail,
            final_status=final.status,
            final_detail=final.detail,
            history_fallback_used=final != current,
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
        print(
            "TASK049_BUFF_DIAGNOSTIC "
            f"current_status={result.current_status.value} "
            f"current_detail={result.current_detail} "
            f"final_status={result.final_status.value} "
            f"final_detail={result.final_detail} "
            f"history_fallback_used={str(result.history_fallback_used).lower()} "
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
