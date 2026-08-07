from __future__ import annotations

import ast
import importlib
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.auto_offer import (
    AUTO_OFFER_DEFAULT_ENABLED,
    AUTO_OFFER_MODULE_ID,
    AUTO_OFFER_STAGE,
)
from app.auto_offer.contracts import (
    DELIVERY_ERROR_CODES,
    TERMINAL_DELIVERY_STATUSES,
    AutoOfferResult,
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    result_blocks_next_purchase,
    validate_delivery_snapshot,
    validate_delivery_transition,
)


def snapshot(**changes: object) -> DeliverySnapshot:
    values: dict[str, object] = {
        "purchase_id": "purchase-1",
        "buff_order_id": "buff-order-1",
        "account_id": "account-1",
        "recipient_steam_id": "76561198000000000",
        "delivery_mode": DeliveryMode.BUYER_SENDS_OFFER,
        "delivery_status": DeliveryStatus.AWAITING_OFFER,
        "steam_tradeoffer_id": None,
        "offer_attempted_at": None,
        "offer_sent_at": None,
        "received_at": None,
        "delivery_error": None,
        "pending_receipt": True,
        "assetid": None,
    }
    values.update(changes)
    return DeliverySnapshot(**values)


def test_module_constants_are_frozen_by_contract():
    assert AUTO_OFFER_MODULE_ID == "action.auto_offer_delivery"
    assert AUTO_OFFER_STAGE == "buy.purchase_committed"
    assert AUTO_OFFER_DEFAULT_ENABLED is False


def test_auto_offer_result_values_are_exact_and_have_no_extras():
    assert [item.value for item in AutoOfferResult] == [
        "disabled",
        "complete",
        "waiting",
        "result_unknown",
        "blocked",
    ]


@pytest.mark.parametrize(
    ("result", "blocks"),
    [
        (AutoOfferResult.DISABLED, False),
        (AutoOfferResult.COMPLETE, False),
        (AutoOfferResult.WAITING, True),
        (AutoOfferResult.RESULT_UNKNOWN, True),
        (AutoOfferResult.BLOCKED, True),
    ],
)
def test_result_gate_mapping(result: AutoOfferResult, blocks: bool):
    assert result_blocks_next_purchase(result) is blocks


@pytest.mark.parametrize("value", ["waiting", None, object(), 1])
def test_unknown_result_fails_closed(value: object):
    with pytest.raises(DeliveryContractError):
        result_blocks_next_purchase(value)  # type: ignore[arg-type]


def test_delivery_mode_values_are_exact():
    assert [item.value for item in DeliveryMode] == [
        "seller_sends_offer",
        "buyer_sends_offer",
    ]


def test_delivery_status_values_are_exact():
    assert [item.value for item in DeliveryStatus] == [
        "pending_direction",
        "awaiting_offer",
        "offer_attempted",
        "offer_sent",
        "offer_received",
        "offer_confirmed",
        "awaiting_inventory",
        "received",
        "result_unknown",
        "blocked",
        "cancelled",
        "refunded",
    ]


def test_snapshot_is_frozen():
    value = snapshot()
    with pytest.raises(FrozenInstanceError):
        value.delivery_status = DeliveryStatus.BLOCKED  # type: ignore[misc]


def test_buyer_path_is_valid():
    statuses = [
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    for index, status in enumerate(statuses):
        value = snapshot(
            delivery_mode=None if index == 0 else DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=status,
            offer_attempted_at=10.0 if index >= 2 else None,
            offer_sent_at=11.0 if index >= 3 else None,
            steam_tradeoffer_id=(
                "trade-1"
                if status
                in {
                    DeliveryStatus.OFFER_SENT,
                    DeliveryStatus.OFFER_CONFIRMED,
                    DeliveryStatus.AWAITING_INVENTORY,
                    DeliveryStatus.RECEIVED,
                }
                else None
            ),
            assetid="asset-1" if status is DeliveryStatus.RECEIVED else None,
            received_at=12.0 if status is DeliveryStatus.RECEIVED else None,
            pending_receipt=status is not DeliveryStatus.RECEIVED,
        )
        validate_delivery_snapshot(value)


def test_seller_path_is_valid():
    statuses = [
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    for index, status in enumerate(statuses):
        value = snapshot(
            delivery_mode=None if index == 0 else DeliveryMode.SELLER_SENDS_OFFER,
            delivery_status=status,
            steam_tradeoffer_id=(
                "trade-1"
                if status
                in {
                    DeliveryStatus.OFFER_RECEIVED,
                    DeliveryStatus.OFFER_CONFIRMED,
                    DeliveryStatus.AWAITING_INVENTORY,
                    DeliveryStatus.RECEIVED,
                }
                else None
            ),
            assetid="asset-1" if status is DeliveryStatus.RECEIVED else None,
            received_at=12.0 if status is DeliveryStatus.RECEIVED else None,
            pending_receipt=status is not DeliveryStatus.RECEIVED,
        )
        validate_delivery_snapshot(value)


@pytest.mark.parametrize(
    ("current", "target", "mode"),
    [
        (
            DeliveryStatus.AWAITING_OFFER,
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.BUYER_SENDS_OFFER,
        ),
        (
            DeliveryStatus.AWAITING_OFFER,
            DeliveryStatus.OFFER_ATTEMPTED,
            DeliveryMode.SELLER_SENDS_OFFER,
        ),
        (
            DeliveryStatus.AWAITING_OFFER,
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.SELLER_SENDS_OFFER,
        ),
        (
            DeliveryStatus.RESULT_UNKNOWN,
            DeliveryStatus.OFFER_ATTEMPTED,
            DeliveryMode.BUYER_SENDS_OFFER,
        ),
    ],
)
def test_invalid_direction_or_resend_transition_is_rejected(
    current: DeliveryStatus, target: DeliveryStatus, mode: DeliveryMode
):
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(current, target, mode)


def test_buyer_status_transitions_are_forward_only():
    statuses = [
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    for current, target in zip(statuses, statuses[1:]):
        validate_delivery_transition(current, target, DeliveryMode.BUYER_SENDS_OFFER)


def test_seller_status_transitions_are_forward_only():
    statuses = [
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    for current, target in zip(statuses, statuses[1:]):
        validate_delivery_transition(current, target, DeliveryMode.SELLER_SENDS_OFFER)


@pytest.mark.parametrize("terminal", list(TERMINAL_DELIVERY_STATUSES))
def test_terminal_status_cannot_transition(terminal: DeliveryStatus):
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(terminal, DeliveryStatus.BLOCKED, DeliveryMode.BUYER_SENDS_OFFER)


def test_result_unknown_can_only_recover_with_later_evidence():
    validate_delivery_transition(
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryStatus.OFFER_SENT,
        DeliveryMode.BUYER_SENDS_OFFER,
    )
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(
            DeliveryStatus.RESULT_UNKNOWN,
            DeliveryStatus.AWAITING_OFFER,
            DeliveryMode.BUYER_SENDS_OFFER,
        )


def test_pending_direction_requires_unknown_mode():
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.PENDING_DIRECTION))


def test_awaiting_offer_requires_known_mode():
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(delivery_mode=None))


@pytest.mark.parametrize("field", ["purchase_id", "buff_order_id", "account_id", "recipient_steam_id"])
@pytest.mark.parametrize("bad", ["", "   ", " id", "id ", " id "])
def test_required_ids_are_strict_and_trim_free(field: str, bad: str):
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(**{field: bad}))


@pytest.mark.parametrize("field", ["steam_tradeoffer_id", "assetid"])
def test_optional_ids_are_strict_when_present(field: str):
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(**{field: " asset"}))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1, True, "1"])
def test_timestamps_are_finite_nonnegative_nonbool_numbers(value: object):
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(offer_attempted_at=value))


def test_pending_receipt_must_be_bool():
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(pending_receipt=1))


@pytest.mark.parametrize("error", ["free text", "write_result_unknown ", "", 1])
def test_delivery_error_is_allowlisted(error: object):
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(delivery_error=error))


def test_delivery_error_allowlist_is_exact():
    assert DELIVERY_ERROR_CODES == {
        "steam_id_mismatch",
        "contract_unknown",
        "write_result_unknown",
        "offer_not_found",
        "inventory_reconciliation_failed",
        "module_disabled",
        "module_contract_mismatch",
    }


def test_result_unknown_requires_write_result_unknown_and_pending_receipt():
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(
            snapshot(delivery_status=DeliveryStatus.RESULT_UNKNOWN, delivery_error="offer_not_found")
        )
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(
            snapshot(
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
                pending_receipt=False,
            )
        )
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(
            snapshot(
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
                received_at=2.0,
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"steam_tradeoffer_id": None},
        {"assetid": None},
        {"received_at": None},
        {"pending_receipt": True},
    ],
)
def test_received_requires_exact_receipt_proof(changes: dict[str, object]):
    values = {
        "delivery_status": DeliveryStatus.RECEIVED,
        "pending_receipt": False,
        "steam_tradeoffer_id": "trade-1",
        "assetid": "asset-1",
        "received_at": 2.0,
    }
    values.update(changes)
    value = snapshot(**values)
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(value)


def test_non_received_status_requires_pending_receipt():
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(pending_receipt=False))


def test_received_requires_ordered_timestamps():
    value = snapshot(
        delivery_status=DeliveryStatus.RECEIVED,
        pending_receipt=False,
        steam_tradeoffer_id="trade-1",
        assetid="asset-1",
        offer_attempted_at=3.0,
        offer_sent_at=2.0,
        received_at=1.0,
    )
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(value)


def test_snapshot_transition_validates_both_endpoints_and_identity():
    current = snapshot(delivery_status=DeliveryStatus.AWAITING_OFFER)
    target = replace(
        current,
        delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
        offer_attempted_at=1.0,
    )
    validate_delivery_transition(current, target)
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(current, replace(target, purchase_id="other"))


@pytest.mark.parametrize(
    ("target", "mode"),
    [
        (DeliveryStatus.RESULT_UNKNOWN, "buyer_sends_offer"),
        (DeliveryStatus.BLOCKED, "buyer_sends_offer"),
        (DeliveryStatus.CANCELLED, 1),
        (DeliveryStatus.REFUNDED, object()),
    ],
)
def test_exception_targets_reject_non_enum_delivery_mode(
    target: DeliveryStatus, mode: object
):
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(DeliveryStatus.AWAITING_OFFER, target, mode)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "target",
    [
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryStatus.BLOCKED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    ],
)
def test_exception_targets_accept_valid_delivery_mode(target: DeliveryStatus):
    validate_delivery_transition(
        DeliveryStatus.AWAITING_OFFER,
        target,
        DeliveryMode.BUYER_SENDS_OFFER,
    )


@pytest.mark.parametrize(
    ("status", "mode", "valid", "invalid"),
    [
        (
            DeliveryStatus.OFFER_ATTEMPTED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0},
            {"offer_attempted_at": None},
        ),
        (
            DeliveryStatus.OFFER_ATTEMPTED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0},
            {"steam_tradeoffer_id": "trade-1"},
        ),
        (
            DeliveryStatus.OFFER_ATTEMPTED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0},
            {"offer_sent_at": 2.0},
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0, "steam_tradeoffer_id": "trade-1"},
            {"offer_attempted_at": None},
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0, "steam_tradeoffer_id": "trade-1"},
            {"offer_sent_at": None},
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0, "steam_tradeoffer_id": "trade-1"},
            {"steam_tradeoffer_id": None},
        ),
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1"},
            {"steam_tradeoffer_id": None},
        ),
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1"},
            {"offer_attempted_at": 1.0},
        ),
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1"},
            {"offer_sent_at": 2.0},
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1", "offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            {"steam_tradeoffer_id": None},
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1", "offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            {"offer_attempted_at": None},
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1"},
            {"offer_sent_at": 2.0},
        ),
        (
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1", "offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            {"steam_tradeoffer_id": None},
        ),
        (
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1", "offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            {"offer_sent_at": None},
        ),
        (
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "trade-1"},
            {"offer_attempted_at": 1.0},
        ),
    ],
)
def test_intermediate_delivery_field_invariants(
    status: DeliveryStatus,
    mode: DeliveryMode,
    valid: dict[str, object],
    invalid: dict[str, object],
):
    validate_delivery_snapshot(snapshot(delivery_status=status, delivery_mode=mode, **valid))
    changed = dict(valid)
    changed.update(invalid)
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(snapshot(delivery_status=status, delivery_mode=mode, **changed))


def test_received_preserves_buyer_timing_and_rejects_seller_timing():
    validate_delivery_snapshot(
        snapshot(
            delivery_status=DeliveryStatus.RECEIVED,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            offer_attempted_at=1.0,
            offer_sent_at=2.0,
            steam_tradeoffer_id="trade-1",
            assetid="asset-1",
            received_at=3.0,
            pending_receipt=False,
        )
    )
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(
            snapshot(
                delivery_status=DeliveryStatus.RECEIVED,
                delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
                offer_attempted_at=1.0,
                steam_tradeoffer_id="trade-1",
                assetid="asset-1",
                received_at=3.0,
                pending_receipt=False,
            )
        )


def test_recipient_steam_id_is_immutable_across_snapshot_transition():
    current = snapshot(delivery_status=DeliveryStatus.AWAITING_OFFER)
    target = replace(
        current,
        delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
        offer_attempted_at=1.0,
        recipient_steam_id="76561198000000001",
    )
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(current, target)


def test_imports_are_pure_and_do_not_reference_external_trade_or_storage_modules():
    importlib.import_module("app.auto_offer")
    importlib.import_module("app.auto_offer.contracts")
    for module_name in ("app.auto_offer", "app.auto_offer.contracts"):
        module_path = Path(importlib.import_module(module_name).__file__ or "")
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not imported.intersection({"buff", "steam", "sqlite3", "requests"})
