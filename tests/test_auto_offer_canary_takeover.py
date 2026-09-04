from __future__ import annotations

from dataclasses import dataclass

import pytest

import app.auto_offer.canary_takeover as takeover_module
from app.auto_offer.canary_takeover import (
    CanaryTakeover,
    CanaryTakeoverError,
    CanaryTakeoverIntegration,
    CanaryTakeoverPhase,
)
from app.auto_offer.contracts import AutoOfferResult, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.host_integration import DeliveryTickOutcome
from app.auto_offer.store import StoredDelivery


ORDER_ID = "buff-order-7"
ACCOUNT_ID = "account-1"
RECIPIENT = "76561198000000007"
COUNTERPARTY = "76561198000000008"


def _stored(status: DeliveryStatus = DeliveryStatus.PENDING_DIRECTION) -> StoredDelivery:
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{ORDER_ID}",
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT,
            delivery_mode=None if status is DeliveryStatus.PENDING_DIRECTION else DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=status is not DeliveryStatus.RECEIVED,
            assetid=None,
        ),
        revision=1,
    )


def _host_row(order_id: str = ORDER_ID, db_id: int = 7) -> dict:
    return {
        "_db_id": db_id,
        "buff_order_id": order_id,
        "pending_receipt": True,
        "assetid": None,
    }


@dataclass
class _NormalIntegration:
    stored: StoredDelivery | None
    recoverable: tuple[StoredDelivery, ...]
    account_id: str = ACCOUNT_ID
    recipient_steam_id: str = RECIPIENT
    closed: int = 0

    def get_by_purchase_id(self, purchase_id: str):
        assert purchase_id == f"buff:{ORDER_ID}"
        return self.stored

    def list_recoverable(self):
        return self.recoverable

    def run_delivery_tick(self, purchases, *, cursor=None):
        if (
            self.stored is not None
            and self.stored.snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION
        ):
            self.stored = _stored(DeliveryStatus.AWAITING_OFFER)
            self.recoverable = (self.stored,)
        return DeliveryTickOutcome(AutoOfferResult.WAITING, ORDER_ID, (ORDER_ID,))

    def close(self):
        self.closed += 1


class _OwnerIntegration:
    def __init__(self, result: AutoOfferResult = AutoOfferResult.WAITING):
        self.result = result
        self.closed = 0

    def next_purchase_result(self, purchases):
        return self.result

    def close(self):
        self.closed += 1


def _controller(host_rows: list[dict], store_rows=()):
    return CanaryTakeover(
        host_purchases_provider=lambda: list(host_rows),
        store_rows_provider=lambda: list(store_rows),
        checkout_provider=lambda: None,
        clock=lambda: 123.0,
    )


def _prepare(controller: CanaryTakeover) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(takeover_module, "canary_metadata_present", lambda: False)
    try:
        controller.prepare()
    finally:
        monkeypatch.undo()


def test_prepare_and_cancel_are_ephemeral_and_read_only():
    calls = {"host": 0, "store": 0}

    def host():
        calls["host"] += 1
        return []

    def store():
        calls["store"] += 1
        return []

    controller = CanaryTakeover(
        host_purchases_provider=host,
        store_rows_provider=store,
        checkout_provider=lambda: None,
    )
    _prepare(controller)
    assert controller.phase is CanaryTakeoverPhase.PREPARED
    assert calls == {"host": 1, "store": 1}
    assert controller.cancel().phase is CanaryTakeoverPhase.IDLE


def test_prepare_fails_closed_on_nonterminal_store_row():
    controller = _controller([], [_stored()])
    with pytest.raises(CanaryTakeoverError, match="canary_prepare_store_not_quiet"):
        _prepare(controller)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_prepare_fails_closed_on_unresolved_checkout():
    controller = CanaryTakeover(
        host_purchases_provider=lambda: [],
        store_rows_provider=lambda: [],
        checkout_provider=lambda: {"stage": "write_result_unknown"},
    )
    with pytest.raises(CanaryTakeoverError, match="canary_prepare_checkout_unresolved"):
        _prepare(controller)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_capture_activates_one_exact_target_and_retains_owner():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))
    owner = _OwnerIntegration(AutoOfferResult.COMPLETE)
    built = []

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda permit: (built.append(permit) or owner),
    )
    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert len(built) == 1
    assert normal.closed == 1
    assert controller.owner_active

    outcome = controller.run_owner_tick(host_rows)
    assert type(outcome) is DeliveryTickOutcome
    assert outcome.result is AutoOfferResult.COMPLETE
    assert owner.closed == 1
    assert controller.phase is CanaryTakeoverPhase.COMPLETE


def test_capture_rejects_zero_or_multiple_committed_targets_and_fences():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    normal = _NormalIntegration(_stored(), (_stored(),))
    with pytest.raises(CanaryTakeoverError, match="canary_multiple_committed_purchases"):
        controller.capture_committed_purchases(
            (),
            normal_integration=normal,
            build_canary_integration=lambda permit: _OwnerIntegration(),
        )
    assert controller.phase is CanaryTakeoverPhase.ABORTED


def test_capture_rejects_unresolved_checkout_after_bounded_reconcile():
    host_rows: list[dict] = []
    checkout = [None]
    controller = CanaryTakeover(
        host_purchases_provider=lambda: list(host_rows),
        store_rows_provider=lambda: [],
        checkout_provider=lambda: checkout[0],
    )
    _prepare(controller)
    host_rows.append(_host_row())
    checkout[0] = {"stage": "order_created_pending"}
    normal = _NormalIntegration(_stored(), (_stored(),))
    with pytest.raises(CanaryTakeoverError, match="canary_checkout_unresolved"):
        controller.capture_committed_purchases(
            ({"buff_order_id": ORDER_ID},),
            normal_integration=normal,
            build_canary_integration=lambda permit: _OwnerIntegration(),
            reconcile_checkout=lambda: None,
        )
    assert controller.phase is CanaryTakeoverPhase.ABORTED


def test_wrapper_does_not_close_retained_owner():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))
    owner = _OwnerIntegration()
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda permit: owner,
    )
    wrapper = CanaryTakeoverIntegration(controller, normal)
    wrapper.close()
    assert owner.closed == 0
    assert controller.owner_active


def test_wrapper_blocks_any_second_purchase_after_capture():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))
    wrapper = CanaryTakeoverIntegration(controller, normal)
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda permit: _OwnerIntegration(),
    )
    assert wrapper.next_purchase_result(host_rows) is AutoOfferResult.BLOCKED
    with pytest.raises(CanaryTakeoverError, match="canary_second_purchase_forbidden"):
        wrapper.register_committed_purchase({"buff_order_id": "buff-order-8"})



def test_prepare_has_no_target_specific_identity_or_direction():
    controller = _controller([])
    _prepare(controller)
    status = controller.status()
    assert status.phase is CanaryTakeoverPhase.PREPARED
    assert status.expected_counterparty_steam_id is None
    assert status.expected_is_our_offer is None


def test_capture_waits_fenced_until_existing_direction_read_proves_target():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())

    class WaitingNormal(_NormalIntegration):
        def run_delivery_tick(self, purchases, *, cursor=None):
            return DeliveryTickOutcome(AutoOfferResult.WAITING, ORDER_ID, (ORDER_ID,))

    normal = WaitingNormal(_stored(), (_stored(),))
    built = []
    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda permit: (built.append(permit) or _OwnerIntegration()),
    )
    assert status.phase is CanaryTakeoverPhase.TARGET_CAPTURED
    assert controller.purchase_blocked is True
    assert built == []

    normal.stored = _stored(DeliveryStatus.AWAITING_OFFER)
    normal.recoverable = (normal.stored,)
    outcome = controller.run_capture_binding_tick(host_rows)
    assert outcome.result is AutoOfferResult.WAITING
    assert controller.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert len(built) == 1
    assert built[0].expected_counterparty_steam_id is None
    assert built[0].expected_is_our_offer is True
