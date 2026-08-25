"""One-shot exact Steam manual closeout for the historical TASK-049 delivery.

This is an explicit maintenance command, not a production recovery path.  The
OWNER supplies one exact Steam Trade Offer ID.  The command proves that exact
ID twice through existing GET-only Steam readers: first as the authenticated
account's outgoing accepted offer, then as one completed trade with an exact
receipt mapping.  Only after all read evidence and the original fingerprint
binding remain stable does it advance the existing Store through contract-valid
local CAS transitions and perform the existing exact Host receipt handoff.

No BUFF request, SEND, ACCEPT, CONFIRM, purchase, payment, listing, delist, or
other platform write capability is present here.  Current-inventory presence is
not required for this historical maintenance because the exact Steam receipt
already proves the post-trade asset ID and the asset may have been sold after
receipt; any inventory evidence that is present must still agree exactly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
    SteamCompletedTradeEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)
from app.auto_offer.contracts import (
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from app.auto_offer.platform_readonly import (
    SteamCompletedTradeReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)
from app.auto_offer.recovery_command import (
    RecoveryCommandError,
    RecoveryTargetBinding,
    _HOST_DB_PATH,
    _STORE_PATH,
    _assert_binding_stable,
    _assert_exact_host_order_readonly,
    _assert_store_preexecution_stable,
    _strict_hex,
    collect_recovery_preflight,
)
from app.auto_offer.steam_readonly_transport import (
    SteamCompletedTradeHttpReader,
    SteamTradeOfferHttpReader,
)
from app.auto_offer.store import AutoOfferStore, StoredDelivery
from app.database import db_complete_purchase_receipt_by_id


@dataclass(frozen=True, slots=True)
class ManualCloseoutProof:
    offer: SteamTradeOfferEvidence
    completed: SteamCompletedTradeEvidence


def _fail(reason: str) -> RecoveryCommandError:
    return RecoveryCommandError(reason)


def _canonical_positive_decimal(value: object, reason: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise _fail(reason)
    number = int(value)
    if number <= 0 or str(number) != value:
        raise _fail(reason)
    return value


def _request(
    binding: RecoveryTargetBinding,
    *,
    capability: PlatformCapability,
    steam_tradeoffer_id: str,
) -> PlatformRequest:
    snapshot = binding.store.snapshot
    return PlatformRequest(
        purchase_id=snapshot.purchase_id,
        buff_order_id=snapshot.buff_order_id,
        account_id=binding.account_id,
        recipient_steam_id=binding.recipient_steam_id,
        revision=binding.store.revision,
        capability=capability,
        timeout_seconds=20.0,
        steam_tradeoffer_id=steam_tradeoffer_id,
    )


def _source_item_identity(item: object) -> tuple[object, object, object, object]:
    return (
        getattr(item, "appid", None),
        getattr(item, "contextid", None),
        getattr(item, "assetid", None),
        getattr(item, "amount", None),
    )


def collect_manual_closeout_proof(
    binding: RecoveryTargetBinding,
    steam_tradeoffer_id: str,
    *,
    trade_offer_reader: object | None = None,
    completed_trade_reader: object | None = None,
) -> ManualCloseoutProof:
    """Prove one exact historical buyer offer and completed receipt, read-only."""

    offer_id = _canonical_positive_decimal(
        steam_tradeoffer_id,
        "steam_tradeoffer_id_invalid",
    )
    offer_reader = (
        SteamTradeOfferHttpReader(binding.steam_cookie)
        if trade_offer_reader is None
        else trade_offer_reader
    )
    offer_adapter = SteamTradeOfferReadOnlyAdapter(
        offer_reader,
        account_id=binding.account_id,
        recipient_steam_id=binding.recipient_steam_id,
    )
    offer_result = offer_adapter.execute(
        _request(
            binding,
            capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_tradeoffer_id=offer_id,
        )
    )
    if offer_result.status is not PlatformResultStatus.SUCCESS:
        raise _fail("steam_trade_offer_not_proven")
    offer = offer_result.evidence
    if type(offer) is not SteamTradeOfferEvidence:
        raise _fail("steam_trade_offer_not_proven")
    if (
        offer.steam_tradeoffer_id != offer_id
        or offer.account_steam_id != binding.recipient_steam_id
        or offer.is_our_offer is not True
        or offer.lifecycle is not SteamTradeOfferLifecycle.ACCEPTED
        or offer.items_to_give != ()
        or len(offer.items_to_receive) != 1
    ):
        raise _fail("steam_trade_offer_identity_mismatch")

    completed_reader = (
        SteamCompletedTradeHttpReader(binding.steam_cookie)
        if completed_trade_reader is None
        else completed_trade_reader
    )
    completed_adapter = SteamCompletedTradeReadOnlyAdapter(
        completed_reader,
        account_id=binding.account_id,
        recipient_steam_id=binding.recipient_steam_id,
    )
    completed_result = completed_adapter.execute(
        _request(
            binding,
            capability=PlatformCapability.READ_STEAM_COMPLETED_TRADE,
            steam_tradeoffer_id=offer_id,
        )
    )
    if completed_result.status is not PlatformResultStatus.SUCCESS:
        raise _fail("steam_completed_trade_not_proven")
    completed = completed_result.evidence
    if type(completed) is not SteamCompletedTradeEvidence:
        raise _fail("steam_completed_trade_not_proven")
    if (
        completed.steam_tradeoffer_id != offer_id
        or completed.account_steam_id != binding.recipient_steam_id
        or completed.counterparty_steam_id != offer.counterparty_steam_id
        or completed.items_given != ()
        or len(completed.items_received) != 1
        or _source_item_identity(completed.items_received[0])
        != _source_item_identity(offer.items_to_receive[0])
    ):
        raise _fail("steam_completed_trade_identity_mismatch")

    received = completed.items_received[0]
    expected_inventory = (
        received.appid,
        received.new_contextid,
        received.new_assetid,
        received.amount,
    )
    for item in completed.inventory_confirmed_items:
        current = (item.appid, item.contextid, item.assetid, item.amount)
        if current != expected_inventory:
            raise _fail("steam_inventory_identity_mismatch")

    attempted_at = binding.store.snapshot.offer_attempted_at
    if (
        attempted_at is None
        or completed.completed_at < attempted_at
    ):
        raise _fail("steam_completed_time_precedes_attempt")
    return ManualCloseoutProof(offer=offer, completed=completed)


def _targets(
    binding: RecoveryTargetBinding,
    proof: ManualCloseoutProof,
) -> tuple[DeliverySnapshot, DeliverySnapshot, DeliverySnapshot, DeliverySnapshot]:
    current = binding.store.snapshot
    completed_at = float(proof.completed.completed_at)
    received_item = proof.completed.items_received[0]

    # ``offer_sent_at`` is a recovery proof timestamp, not a claim about the
    # original HTTP POST time.  Exact Steam completion proves the offer had
    # already been sent by ``completed_at``; using the same proven timestamp as
    # ``received_at`` preserves the contract ordering without inventing a time.
    sent = replace(
        current,
        delivery_status=DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id=proof.offer.steam_tradeoffer_id,
        counterparty_steam_id=proof.offer.counterparty_steam_id,
        offer_sent_at=completed_at,
        delivery_error=None,
    )
    confirmed = replace(sent, delivery_status=DeliveryStatus.OFFER_CONFIRMED)
    awaiting = replace(
        confirmed,
        delivery_status=DeliveryStatus.AWAITING_INVENTORY,
    )
    received = replace(
        awaiting,
        delivery_status=DeliveryStatus.RECEIVED,
        received_at=completed_at,
        pending_receipt=False,
        assetid=received_item.new_assetid,
    )

    previous = current
    for target in (sent, confirmed, awaiting, received):
        validate_delivery_snapshot(target)
        validate_delivery_transition(previous, target)
        previous = target
    return sent, confirmed, awaiting, received


def execute_manual_closeout(
    binding: RecoveryTargetBinding,
    *,
    expected_fingerprint: str,
    steam_tradeoffer_id: str,
    store_path: Path = _STORE_PATH,
    host_db_path: Path = _HOST_DB_PATH,
    receipt_writer=db_complete_purchase_receipt_by_id,
    trade_offer_reader: object | None = None,
    completed_trade_reader: object | None = None,
) -> StoredDelivery:
    """Perform one evidence-first historical closeout and exact Host handoff."""

    expected = _strict_hex(
        expected_fingerprint,
        64,
        "expected_fingerprint_invalid",
    )
    if binding.fingerprint != expected:
        raise _fail("target_fingerprint_mismatch")
    offer_id = _canonical_positive_decimal(
        steam_tradeoffer_id,
        "steam_tradeoffer_id_invalid",
    )

    # Re-prove the exact local target before any network read.
    _assert_binding_stable(binding)
    _assert_store_preexecution_stable(binding)

    proof = collect_manual_closeout_proof(
        binding,
        offer_id,
        trade_offer_reader=trade_offer_reader,
        completed_trade_reader=completed_trade_reader,
    )
    targets = _targets(binding, proof)

    # Network reads may take time. Re-prove the same fingerprint-bound local
    # source immediately before opening the existing Store RW.
    _assert_binding_stable(binding)
    _assert_store_preexecution_stable(binding)

    store = AutoOfferStore(store_path)
    try:
        store.initialize_existing()
        current = store.get_by_buff_order_id(binding.order_id)
        if current != binding.store:
            raise _fail("store_target_changed_before_cas")
        for target in targets:
            current = store.advance(current, target)
        final = current
    finally:
        store.close()

    final_assetid = final.snapshot.assetid
    if (
        final.snapshot.delivery_status is not DeliveryStatus.RECEIVED
        or final.snapshot.pending_receipt is not False
        or type(final_assetid) is not str
        or not final_assetid
    ):
        raise _fail("store_closeout_not_received")

    _assert_exact_host_order_readonly(
        host_db_path,
        order_id=binding.order_id,
        expected_db_id=binding.host_db_id,
        expected_pending=True,
        expected_assetid=None,
    )
    if not receipt_writer(
        binding.host_db_id,
        binding.order_id,
        final_assetid,
    ):
        # Store is already exact RECEIVED here.  Do not roll it backward or
        # attempt any platform action; a Host-only receipt handoff can be
        # resumed explicitly if this rare local failure occurs.
        raise _fail("host_receipt_completion_failed_after_store_received")
    return final


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.auto_offer.manual_closeout",
        description="One-shot exact Steam historical closeout for TASK-049.",
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--steam-tradeoffer-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        binding = collect_recovery_preflight(
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
        )
        final = execute_manual_closeout(
            binding,
            expected_fingerprint=args.expected_fingerprint,
            steam_tradeoffer_id=args.steam_tradeoffer_id,
        )
        print(
            "TASK049_MANUAL_CLOSEOUT_COMPLETE "
            f"store_revision={final.revision} "
            "store_status=received host_pending=false host_asset_bound=true "
            "buff_requests=0 platform_writes=0"
        )
        return 0
    except RecoveryCommandError as exc:
        print(f"TASK049_MANUAL_CLOSEOUT_BLOCKED reason={exc}")
        return 2
    except Exception:
        print("TASK049_MANUAL_CLOSEOUT_BLOCKED reason=unexpected_closeout_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
