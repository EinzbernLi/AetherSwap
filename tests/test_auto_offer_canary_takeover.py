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
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.host_integration import DeliveryTickOutcome
from app.auto_offer.store import StoredDelivery


ORDER_ID = "buff-order-7"
ACCOUNT_ID = "account-1"
RECIPIENT = "76561198000000007"
COUNTERPARTY = "76561198000000008"


def _stored(
    status: DeliveryStatus = DeliveryStatus.PENDING_DIRECTION,
    *,
    mode: DeliveryMode | None = None,
    counterparty: str | None = None,
) -> StoredDelivery:
    if status is not DeliveryStatus.PENDING_DIRECTION and mode is None:
        mode = DeliveryMode.BUYER_SENDS_OFFER
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{ORDER_ID}",
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=status is not DeliveryStatus.RECEIVED,
            assetid=None,
            counterparty_steam_id=counterparty,
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
    ticks: int = 0
    direction_wait: bool = False
    direction_mode: DeliveryMode = DeliveryMode.BUYER_SENDS_OFFER
    post_direction_result: AutoOfferResult = AutoOfferResult.WAITING

    def get_by_purchase_id(self, purchase_id: str):
        assert purchase_id == f"buff:{ORDER_ID}"
        return self.stored

    def list_recoverable(self):
        return self.recoverable

    def run_delivery_tick(self, purchases, *, cursor=None):
        self.ticks += 1
        if (
            self.stored is not None
            and self.stored.snapshot.delivery_status
            is DeliveryStatus.PENDING_DIRECTION
        ):
            if self.direction_wait:
                return DeliveryTickOutcome(
                    AutoOfferResult.WAITING,
                    ORDER_ID,
                    (ORDER_ID,),
                )
            counterparty = (
                COUNTERPARTY
                if self.direction_mode is DeliveryMode.SELLER_SENDS_OFFER
                else None
            )
            self.stored = _stored(
                DeliveryStatus.AWAITING_OFFER,
                mode=self.direction_mode,
                counterparty=counterparty,
            )
            self.recoverable = (self.stored,)
            return DeliveryTickOutcome(
                AutoOfferResult.WAITING,
                ORDER_ID,
                (ORDER_ID,),
            )
        return DeliveryTickOutcome(
            self.post_direction_result,
            ORDER_ID
            if self.post_direction_result is not AutoOfferResult.COMPLETE
            else None,
            (ORDER_ID,)
            if self.post_direction_result is not AutoOfferResult.COMPLETE
            else (),
        )

    def next_purchase_result(self, purchases):
        return AutoOfferResult.WAITING

    def register_committed_purchase(self, purchase):
        return None

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
    monkeypatch.setattr(
        takeover_module,
        "canary_metadata_present",
        lambda: False,
    )
    try:
        controller.prepare()
    finally:
        monkeypatch.undo()


def _never_build(_permit):
    raise AssertionError("thin canary must never build a second integration")


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
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_prepare_store_not_quiet",
    ):
        _prepare(controller)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_prepare_fails_closed_on_unresolved_checkout():
    controller = CanaryTakeover(
        host_purchases_provider=lambda: [],
        store_rows_provider=lambda: [],
        checkout_provider=lambda: {"stage": "write_result_unknown"},
    )
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_prepare_checkout_unresolved",
    ):
        _prepare(controller)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_capture_reuses_same_normal_integration_and_never_builds_owner():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(
        _stored(),
        (_stored(),),
        post_direction_result=AutoOfferResult.COMPLETE,
    )

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )

    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert controller.owner_active
    assert controller.active_integration() is normal
    assert normal.closed == 0
    assert normal.ticks == 1

    outcome = controller.run_owner_tick(host_rows)
    assert type(outcome) is DeliveryTickOutcome
    assert outcome.result is AutoOfferResult.COMPLETE
    assert normal.closed == 1
    assert controller.phase is CanaryTakeoverPhase.COMPLETE


def test_capture_binds_buyer_direction_without_permit_or_runtime_swap():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )

    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert status.expected_is_our_offer is True
    assert status.expected_counterparty_steam_id is None
    assert controller.active_integration() is normal
    assert normal.closed == 0


def test_capture_binds_seller_direction_without_runtime_swap():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(
        _stored(),
        (_stored(),),
        direction_mode=DeliveryMode.SELLER_SENDS_OFFER,
    )

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )

    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert status.expected_is_our_offer is False
    assert status.expected_counterparty_steam_id == COUNTERPARTY
    assert controller.active_integration() is normal
    assert normal.closed == 0


def test_capture_rejects_zero_or_multiple_committed_targets_and_fences():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    normal = _NormalIntegration(_stored(), (_stored(),))

    with pytest.raises(
        CanaryTakeoverError,
        match="canary_multiple_committed_purchases",
    ):
        controller.capture_committed_purchases(
            (),
            normal_integration=normal,
            build_canary_integration=_never_build,
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

    with pytest.raises(
        CanaryTakeoverError,
        match="canary_checkout_unresolved",
    ):
        controller.capture_committed_purchases(
            ({"buff_order_id": ORDER_ID},),
            normal_integration=normal,
            build_canary_integration=_never_build,
            reconcile_checkout=lambda: None,
        )
    assert controller.phase is CanaryTakeoverPhase.ABORTED
    # Capture failed before the controller retained the integration; the
    # caller/wrapper still owns normal lifecycle at this point.
    assert normal.closed == 0


def test_wrapper_does_not_close_retained_normal_target_integration():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))

    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )
    wrapper = CanaryTakeoverIntegration(controller, normal)
    wrapper.close()

    assert normal.closed == 0
    assert controller.owner_active
    assert controller.active_integration() is normal


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
        build_canary_integration=_never_build,
    )

    assert (
        wrapper.next_purchase_result(host_rows)
        is AutoOfferResult.BLOCKED
    )
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_second_purchase_forbidden",
    ):
        wrapper.register_committed_purchase(
            {"buff_order_id": "buff-order-8"}
        )


def test_prepare_has_no_target_specific_identity_or_direction():
    controller = _controller([])
    _prepare(controller)
    status = controller.status()

    assert status.phase is CanaryTakeoverPhase.PREPARED
    assert status.expected_counterparty_steam_id is None
    assert status.expected_is_our_offer is None


def test_capture_waits_fenced_until_same_integration_proves_direction():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(
        _stored(),
        (_stored(),),
        direction_wait=True,
    )

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )

    assert status.phase is CanaryTakeoverPhase.TARGET_CAPTURED
    assert controller.purchase_blocked is True
    assert controller.active_integration() is None
    assert normal.closed == 0
    assert normal.ticks == 1

    normal.direction_wait = False
    outcome = controller.run_capture_binding_tick(host_rows)

    assert outcome.result is AutoOfferResult.WAITING
    assert controller.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert controller.active_integration() is normal
    assert controller.status().expected_is_our_offer is True
    assert normal.closed == 0
    assert normal.ticks == 2


def test_direction_block_aborts_and_closes_same_integration_without_builder():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())

    class BlockingNormal(_NormalIntegration):
        def run_delivery_tick(self, purchases, *, cursor=None):
            self.ticks += 1
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                ORDER_ID,
                (ORDER_ID,),
            )

    normal = BlockingNormal(_stored(), (_stored(),))

    with pytest.raises(
        CanaryTakeoverError,
        match="canary_direction_read_blocked",
    ):
        controller.capture_committed_purchases(
            ({"buff_order_id": ORDER_ID},),
            normal_integration=normal,
            build_canary_integration=_never_build,
        )

    assert controller.phase is CanaryTakeoverPhase.ABORTED
    assert normal.closed == 1
    assert normal.stored == _stored()


def test_active_target_rejects_any_unrelated_pending_host_row():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(_stored(), (_stored(),))

    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )
    host_rows.append(_host_row("unrelated-order", 8))

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.BLOCKED
    assert controller.phase is CanaryTakeoverPhase.ABORTED
    assert normal.closed == 1


def test_active_target_allows_host_row_to_disappear_only_for_normal_terminal_check():
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    _prepare(controller)
    host_rows.append(_host_row())
    normal = _NormalIntegration(
        _stored(),
        (_stored(),),
        post_direction_result=AutoOfferResult.COMPLETE,
    )

    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=_never_build,
    )
    host_rows.clear()

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.COMPLETE
    assert controller.phase is CanaryTakeoverPhase.COMPLETE
    assert normal.closed == 1
