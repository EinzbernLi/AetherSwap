from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace

import pytest

import app.auto_offer.canary_receive as receive_module
import app.auto_offer.canary_takeover as takeover_module
from app.auto_offer.canary_receive import CanaryReceiveTickResult
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
from app.auto_offer.runtime_mode import AutoOfferRuntimeMode
from app.auto_offer.store import AutoOfferStore, StoredDelivery


ORDER_ID = "buff-order-7"
ACCOUNT_ID = "account-1"
RECIPIENT = "76561198000000007"
COUNTERPARTY = "76561198000000008"


def _stored(
    status: DeliveryStatus = DeliveryStatus.PENDING_DIRECTION,
    *,
    mode: DeliveryMode | None = None,
    counterparty: str | None = None,
    revision: int = 1,
    assetid: str | None = None,
) -> StoredDelivery:
    if status is not DeliveryStatus.PENDING_DIRECTION and mode is None:
        mode = DeliveryMode.BUYER_SENDS_OFFER
    received = status is DeliveryStatus.RECEIVED
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
            received_at=123.0 if received else None,
            delivery_error=None,
            pending_receipt=not received,
            assetid=assetid if received else None,
            counterparty_steam_id=counterparty,
        ),
        revision=revision,
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
    close_thread: int | None = None
    ticks: int = 0

    def get_by_purchase_id(self, purchase_id: str):
        assert purchase_id == f"buff:{ORDER_ID}"
        return self.stored

    def list_recoverable(self):
        return self.recoverable

    def run_delivery_tick(self, purchases, *, cursor=None):
        self.ticks += 1
        raise AssertionError(
            "pipeline-owned integration must not run post-capture delivery"
        )

    def next_purchase_result(self, purchases):
        return AutoOfferResult.WAITING

    def register_committed_purchase(self, purchase):
        return None

    def close(self):
        self.closed += 1
        self.close_thread = threading.get_ident()


def _controller(host_rows: list[dict], store_rows=None, *, fence_path=None):
    rows = [] if store_rows is None else store_rows
    return CanaryTakeover(
        host_purchases_provider=lambda: list(host_rows),
        store_rows_provider=lambda: list(rows),
        checkout_provider=lambda: None,
        clock=lambda: 123.0,
        target_fence_path=fence_path,
    )


def _prepare(controller: CanaryTakeover, monkeypatch) -> None:
    monkeypatch.setattr(
        takeover_module,
        "canary_metadata_present",
        lambda: False,
    )
    controller.prepare()


def _capture_pending(controller, host_rows, monkeypatch, normal=None):
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())
    normal = normal or _NormalIntegration(_stored(), (_stored(),))
    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
        build_canary_integration=lambda _permit: pytest.fail(
            "TASK-100 must never build a second owner integration"
        ),
    )
    return normal, status


def _tick(
    stored: StoredDelivery,
    *,
    result: AutoOfferResult = AutoOfferResult.WAITING,
    reason: str | None = None,
    mode: AutoOfferRuntimeMode = AutoOfferRuntimeMode.ON,
):
    return CanaryReceiveTickResult(
        outcome=DeliveryTickOutcome(
            result,
            ORDER_ID if result is not AutoOfferResult.COMPLETE else None,
            (ORDER_ID,),
        ),
        stored=stored,
        runtime_mode=mode,
        reason=reason,
    )


def test_prepare_and_cancel_are_quiet_and_ephemeral_without_production_fence(
    monkeypatch,
):
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
    _prepare(controller, monkeypatch)
    assert controller.phase is CanaryTakeoverPhase.PREPARED
    assert calls == {"host": 1, "store": 1}
    assert controller.cancel().phase is CanaryTakeoverPhase.IDLE


def test_prepare_fails_closed_on_nonterminal_store_row(monkeypatch):
    controller = _controller([], [_stored()])
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_prepare_store_not_quiet",
    ):
        _prepare(controller, monkeypatch)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_prepare_fails_closed_on_unresolved_checkout(monkeypatch):
    controller = CanaryTakeover(
        host_purchases_provider=lambda: [],
        store_rows_provider=lambda: [],
        checkout_provider=lambda: {"stage": "write_result_unknown"},
    )
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_prepare_checkout_unresolved",
    ):
        _prepare(controller, monkeypatch)
    assert controller.phase is CanaryTakeoverPhase.IDLE


def test_capture_retains_identity_only_and_pipeline_wrapper_closes_origin_resource(
    monkeypatch,
):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    normal, status = _capture_pending(
        controller,
        host_rows,
        monkeypatch,
    )
    wrapper = CanaryTakeoverIntegration(controller, normal)
    origin = threading.get_ident()
    wrapper.close()

    assert status.phase is CanaryTakeoverPhase.TARGET_CAPTURED
    assert controller.active_integration() is None
    assert controller.owner_active is False
    assert normal.ticks == 0
    assert normal.closed == 1
    assert normal.close_thread == origin


def test_capture_can_bind_already_persisted_buyer_direction_without_handoff(
    monkeypatch,
):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )

    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert status.expected_is_our_offer is True
    assert status.expected_counterparty_steam_id is None
    assert controller.active_integration() is None
    assert normal.ticks == 0


def test_capture_can_bind_already_persisted_seller_direction(monkeypatch):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        counterparty=COUNTERPARTY,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())

    status = controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )

    assert status.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert status.expected_is_our_offer is False
    assert status.expected_counterparty_steam_id == COUNTERPARTY


def test_receive_thread_binds_pending_direction_without_using_pipeline_object(
    monkeypatch,
):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    normal, status = _capture_pending(
        controller,
        host_rows,
        monkeypatch,
    )
    assert status.phase is CanaryTakeoverPhase.TARGET_CAPTURED

    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    receive_threads = []

    def receive_tick(_target, _rows, *, cursor=None):
        receive_threads.append(threading.get_ident())
        return _tick(awaiting)

    monkeypatch.setattr(
        receive_module,
        "run_receive_owned_canary_tick",
        receive_tick,
    )
    outcome = controller.run_capture_binding_tick(host_rows)

    assert outcome.result is AutoOfferResult.WAITING
    assert controller.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert controller.status().expected_is_our_offer is True
    assert controller.active_integration() is None
    assert normal.ticks == 0
    assert receive_threads == [threading.get_ident()]


def test_wrapper_never_dispatches_delivery_after_capture(monkeypatch):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    normal, _ = _capture_pending(
        controller,
        host_rows,
        monkeypatch,
    )
    wrapper = CanaryTakeoverIntegration(controller, normal)

    outcome = wrapper.run_delivery_tick(host_rows)

    assert outcome.result is AutoOfferResult.WAITING
    assert normal.ticks == 0


def test_wrapper_blocks_any_second_purchase_after_capture(monkeypatch):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    normal, _ = _capture_pending(
        controller,
        host_rows,
        monkeypatch,
    )
    wrapper = CanaryTakeoverIntegration(controller, normal)

    assert wrapper.purchase_fence_active is True
    assert wrapper.next_purchase_result(host_rows) is AutoOfferResult.WAITING
    with pytest.raises(
        CanaryTakeoverError,
        match="canary_second_purchase_forbidden",
    ):
        wrapper.register_committed_purchase(
            {"buff_order_id": "buff-order-8"}
        )


def test_capture_rejects_zero_or_multiple_committed_targets_and_fences(
    monkeypatch,
):
    controller = _controller([])
    _prepare(controller, monkeypatch)
    normal = _NormalIntegration(_stored(), (_stored(),))

    with pytest.raises(
        CanaryTakeoverError,
        match="canary_multiple_committed_purchases",
    ):
        controller.capture_committed_purchases(
            (),
            normal_integration=normal,
        )
    assert controller.phase is CanaryTakeoverPhase.ABORTED


def test_capture_rejects_unresolved_checkout_after_bounded_reconcile(
    monkeypatch,
):
    host_rows: list[dict] = []
    checkout = [None]
    controller = CanaryTakeover(
        host_purchases_provider=lambda: list(host_rows),
        store_rows_provider=lambda: [],
        checkout_provider=lambda: checkout[0],
    )
    _prepare(controller, monkeypatch)
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
            reconcile_checkout=lambda: None,
        )
    assert controller.phase is CanaryTakeoverPhase.ABORTED


def test_receive_result_unknown_aborts_with_stable_reason(monkeypatch):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )

    unknown = _stored(
        DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    monkeypatch.setattr(
        receive_module,
        "run_receive_owned_canary_tick",
        lambda *_args, **_kwargs: _tick(
            unknown,
            result=AutoOfferResult.RESULT_UNKNOWN,
            reason="canary_result_unknown",
        ),
    )

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert controller.phase is CanaryTakeoverPhase.ABORTED
    assert controller.status().reason == "canary_result_unknown"


def test_active_target_rejects_unrelated_pending_host_row(monkeypatch):
    host_rows: list[dict] = []
    controller = _controller(host_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )
    host_rows.append(_host_row("unrelated-order", 8))

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.BLOCKED
    assert controller.phase is CanaryTakeoverPhase.ABORTED
    assert (
        controller.status().reason
        == "canary_host_target_not_exclusive"
    )


def test_owner_complete_requires_exact_terminal_store_after_host_receipt_closes(
    monkeypatch,
):
    host_rows: list[dict] = []
    store_rows: list[StoredDelivery] = []
    controller = _controller(host_rows, store_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )

    host_rows.clear()
    store_rows.append(
        _stored(
            DeliveryStatus.RECEIVED,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=8,
            assetid="asset-7",
        )
    )

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.COMPLETE
    assert controller.phase is CanaryTakeoverPhase.COMPLETE


def test_missing_host_without_received_store_fails_closed(monkeypatch):
    host_rows: list[dict] = []
    store_rows: list[StoredDelivery] = []
    controller = _controller(host_rows, store_rows)
    awaiting = _stored(
        DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    normal = _NormalIntegration(awaiting, (awaiting,))
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())
    controller.capture_committed_purchases(
        ({"buff_order_id": ORDER_ID},),
        normal_integration=normal,
    )
    host_rows.clear()
    store_rows.append(
        _stored(
            DeliveryStatus.REFUNDED,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=8,
        )
    )

    outcome = controller.run_owner_tick(host_rows)

    assert outcome.result is AutoOfferResult.BLOCKED
    assert controller.phase is CanaryTakeoverPhase.ABORTED
    assert (
        controller.status().reason
        == "canary_terminal_receipt_not_proven"
    )


def test_prepared_crash_marker_blocks_restart_without_implicit_adoption(
    monkeypatch,
    tmp_path,
):
    fence = tmp_path / "canary-takeover.json"
    controller = _controller([], fence_path=fence)
    _prepare(controller, monkeypatch)

    assert json.loads(fence.read_text(encoding="utf-8")) == {
        "phase": "prepared",
        "version": 1,
    }

    restarted = _controller([], fence_path=fence)
    assert restarted.phase is CanaryTakeoverPhase.ABORTED
    assert (
        restarted.status().reason
        == "canary_restart_recovery_required"
    )
    assert restarted.owner_active is False


def test_target_crash_marker_preserves_identity_but_never_resumes(
    monkeypatch,
    tmp_path,
):
    fence = tmp_path / "canary-takeover.json"
    host_rows: list[dict] = []
    controller = _controller(host_rows, fence_path=fence)
    normal, _ = _capture_pending(
        controller,
        host_rows,
        monkeypatch,
    )
    CanaryTakeoverIntegration(controller, normal).close()

    restarted = _controller(host_rows, fence_path=fence)

    assert restarted.phase is CanaryTakeoverPhase.ABORTED
    status = restarted.status()
    assert status.reason == "canary_restart_recovery_required"
    assert status.buff_order_id == ORDER_ID
    assert status.host_db_id == 7
    assert restarted.owner_active is False
    assert restarted.purchase_blocked is True


def test_restart_fence_requires_explicit_retirement(monkeypatch, tmp_path):
    fence = tmp_path / "canary-takeover.json"
    controller = _controller([], fence_path=fence)
    _prepare(controller, monkeypatch)
    restarted = _controller([], fence_path=fence)

    assert restarted.retire_restart_fence().phase is CanaryTakeoverPhase.IDLE
    assert not fence.exists()


def test_real_sqlite_connections_are_never_crossed_between_pipeline_and_receive(
    monkeypatch,
    tmp_path,
):
    host_rows: list[dict] = []
    db_path = tmp_path / "auto_offer.db"
    controller = _controller(host_rows)
    _prepare(controller, monkeypatch)
    host_rows.append(_host_row())

    close_threads: list[int] = []
    receive_threads: list[int] = []
    holder = {}

    class StoreIntegration:
        account_id = ACCOUNT_ID
        recipient_steam_id = RECIPIENT

        def __init__(self):
            self.store = AutoOfferStore(db_path)
            self.store.initialize()
            self.store.ensure_initial(_stored().snapshot)

        def get_by_purchase_id(self, purchase_id):
            return self.store.get_by_purchase_id(purchase_id)

        def list_recoverable(self):
            return tuple(self.store.list_recoverable())

        def register_committed_purchase(self, _purchase):
            return None

        def next_purchase_result(self, _purchases):
            return AutoOfferResult.WAITING

        def run_delivery_tick(self, *_args, **_kwargs):
            raise AssertionError(
                "pipeline SQLite integration crossed into receive thread"
            )

        def close(self):
            close_threads.append(threading.get_ident())
            self.store.close()

    def pipeline_thread():
        integration = StoreIntegration()
        holder["pipeline_id"] = threading.get_ident()
        holder["integration"] = integration
        wrapper = CanaryTakeoverIntegration(controller, integration)
        controller.capture_committed_purchases(
            ({"buff_order_id": ORDER_ID},),
            normal_integration=integration,
        )
        wrapper.close()

    pipeline = threading.Thread(target=pipeline_thread)
    pipeline.start()
    pipeline.join(timeout=5)
    assert not pipeline.is_alive()
    assert controller.phase is CanaryTakeoverPhase.TARGET_CAPTURED

    def receive_tick(_target, _rows, *, cursor=None):
        receive_threads.append(threading.get_ident())
        store = AutoOfferStore(db_path)
        store.initialize_existing()
        try:
            current = store.get_by_purchase_id(f"buff:{ORDER_ID}")
            assert type(current) is StoredDelivery
            after = store.advance(
                current,
                replace(
                    current.snapshot,
                    delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
                    delivery_status=DeliveryStatus.AWAITING_OFFER,
                ),
            )
        finally:
            store.close()
        return _tick(after)

    monkeypatch.setattr(
        receive_module,
        "run_receive_owned_canary_tick",
        receive_tick,
    )

    def receive_thread():
        holder["receive_id"] = threading.get_ident()
        holder["outcome"] = controller.run_capture_binding_tick(
            host_rows
        )

    receive = threading.Thread(target=receive_thread)
    receive.start()
    receive.join(timeout=5)
    assert not receive.is_alive()

    assert holder["outcome"].result is AutoOfferResult.WAITING
    assert controller.phase is CanaryTakeoverPhase.OWNER_ACTIVE
    assert close_threads == [holder["pipeline_id"]]
    assert receive_threads == [holder["receive_id"]]
    assert holder["pipeline_id"] != holder["receive_id"]
