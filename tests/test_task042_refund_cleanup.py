from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from app import database
from app.auto_offer.adapters import (
    BuffOrderLifecycle,
    BuffOrderLifecycleEvidence,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from app.auto_offer.coordinator import DeliveryCoordinator
from app.auto_offer.host_ownership import (
    HostPurchaseMutationBlockedError,
    require_purchase_append_allowed,
)
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.runtime_lifecycle import inspect_effective_runtime
from app.auto_offer.store import StoredDelivery


def _delivery(status: DeliveryStatus = DeliveryStatus.OFFER_TERMINATED) -> StoredDelivery:
    snapshot = DeliverySnapshot(
        purchase_id="buff:ORDER_A",
        buff_order_id="ORDER_A",
        account_id="account-A",
        recipient_steam_id="76561198000000001",
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=status,
        steam_tradeoffer_id="OFFER_A",
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error="offer_terminated",
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id="76561198000000002",
    )
    validate_delivery_snapshot(snapshot)
    return StoredDelivery(snapshot, 4)


def _lifecycle_result(delivery: StoredDelivery, lifecycle: BuffOrderLifecycle) -> PlatformResult:
    evidence = BuffOrderLifecycleEvidence(
        buff_order_id="ORDER_A",
        lifecycle=lifecycle,
        raw_state="PAYING" if lifecycle is BuffOrderLifecycle.PAYING else "FAIL",
        raw_state_text="等待付款"
        if lifecycle is BuffOrderLifecycle.PAYING
        else "购买失败-已退款",
        page_num=1,
    )
    request = PlatformRequest(
        purchase_id=delivery.snapshot.purchase_id,
        buff_order_id=delivery.snapshot.buff_order_id,
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
        revision=delivery.revision,
        capability=PlatformCapability.READ_BUFF_ORDER_LIFECYCLE,
        timeout_seconds=5.0,
    )
    return PlatformResult(request, PlatformResultStatus.SUCCESS, evidence=evidence)


def test_refunded_read_only_proves_pending_without_same_tick_host_delete():
    current = _delivery()
    decision = plan_read_evidence_transition(
        current,
        _lifecycle_result(current, BuffOrderLifecycle.REFUNDED),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.detail == "refund_proven_cleanup_pending"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.REFUND_CLEANUP_PENDING
    assert decision.target == replace(
        current.snapshot,
        delivery_status=DeliveryStatus.REFUND_CLEANUP_PENDING,
    )


def test_paying_and_wrong_order_do_not_advance_cleanup():
    current = _delivery()
    paying = plan_read_evidence_transition(
        current,
        _lifecycle_result(current, BuffOrderLifecycle.PAYING),
    )
    assert paying.result is AutoOfferResult.WAITING
    assert paying.target is None
    wrong = _lifecycle_result(current, BuffOrderLifecycle.REFUNDED)
    wrong_request = replace(
        wrong.request,
        purchase_id="buff:ORDER_B",
        buff_order_id="ORDER_B",
    )
    wrong_evidence = replace(wrong.evidence, buff_order_id="ORDER_B")
    wrong = PlatformResult(wrong_request, wrong.status, evidence=wrong_evidence)
    decision = plan_read_evidence_transition(current, wrong)
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None


def test_cleanup_graph_and_frozen_snapshot_are_exact():
    terminated = _delivery()
    pending = replace(terminated.snapshot, delivery_status=DeliveryStatus.REFUND_CLEANUP_PENDING)
    refunded = replace(pending, delivery_status=DeliveryStatus.REFUNDED)
    validate_delivery_transition(terminated.snapshot, pending)
    validate_delivery_transition(pending, refunded)
    with pytest.raises(DeliveryContractError):
        validate_delivery_transition(
            terminated.snapshot,
            replace(terminated.snapshot, delivery_status=DeliveryStatus.REFUNDED),
        )
    with pytest.raises(DeliveryContractError, match="delivery_status only"):
        validate_delivery_transition(
            pending,
            replace(refunded, assetid="ASSET_A"),
        )


def test_coordinator_cleanup_uses_one_cas_status_only():
    current = _delivery(DeliveryStatus.REFUND_CLEANUP_PENDING)

    class Store:
        def __init__(self):
            self.advanced = None

        def get_by_purchase_id(self, purchase_id):
            return current if purchase_id == current.snapshot.purchase_id else None

        def advance(self, before, target):
            self.advanced = (before, target)
            return StoredDelivery(target, before.revision + 1)

    store = Store()
    coordinator = DeliveryCoordinator(store, {}, timeout_seconds=5.0)
    result = coordinator.complete_refund_cleanup(current)
    assert result.snapshot == replace(
        current.snapshot,
        delivery_status=DeliveryStatus.REFUNDED,
    )
    assert result.revision == current.revision + 1
    assert store.advanced == (current, result.snapshot)


def test_append_fence_allows_only_legacy_empty_ids_and_missing_exact_id(monkeypatch):
    calls = []

    def inspect(path, order_id):
        calls.append((path, order_id))
        return None

    monkeypatch.setattr(
        "app.auto_offer.host_ownership.AutoOfferStore.inspect_existing_by_buff_order_id",
        inspect,
    )
    require_purchase_append_allowed({"buff_order_id": None})
    require_purchase_append_allowed({"buff_order_id": ""})
    require_purchase_append_allowed({"buff_order_id": "ORDER_A"})
    assert calls and calls[-1][1] == "ORDER_A"
    with pytest.raises(HostPurchaseMutationBlockedError, match="INVALID_BUFF_ORDER_ID"):
        require_purchase_append_allowed({"buff_order_id": " ORDER_A"})
    with pytest.raises(HostPurchaseMutationBlockedError, match="INVALID_BUFF_ORDER_ID"):
        require_purchase_append_allowed({"buff_order_id": 7})


def test_append_fence_rejects_exact_store_reuse(monkeypatch):
    monkeypatch.setattr(
        "app.auto_offer.host_ownership.AutoOfferStore.inspect_existing_by_buff_order_id",
        lambda *_args: _delivery(),
    )
    with pytest.raises(HostPurchaseMutationBlockedError, match="ALREADY_OWNED"):
        require_purchase_append_allowed({"buff_order_id": "ORDER_A"})


def test_cleanup_orphan_is_the_only_protected_store_without_host_exception(monkeypatch):
    monkeypatch.setattr(
        "app.auto_offer.runtime_lifecycle.canary_metadata_present", lambda: False
    )
    cleanup = _delivery(DeliveryStatus.REFUND_CLEANUP_PENDING)
    state = inspect_effective_runtime(
        requested_enabled=False,
        purchases=[],
        store_rows=[cleanup],
        reconciliation_checked=True,
        canary_fenced=False,
    )
    assert state.mode.value == "draining"
    assert state.active_delivery_count == 1
    blocked = inspect_effective_runtime(
        requested_enabled=False,
        purchases=[],
        store_rows=[_delivery(DeliveryStatus.OFFER_TERMINATED)],
        reconciliation_checked=True,
        canary_fenced=False,
    )
    assert blocked.mode.value == "blocked"
    assert blocked.reason == "active_orphan_store"


@pytest.fixture
def isolated_host_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)
    database.init_db()
    yield
    engine = database.get_engine()
    engine.dispose()


def test_host_cleanup_primitive_requires_exact_cardinality_and_pending_state(
    isolated_host_db,
):
    database.db_append_purchase(
        {"buff_order_id": "ORDER_A", "pending_receipt": True, "assetid": None}
    )
    assert database.db_delete_refund_cleanup_purchase("ORDER_A", False) is False
    assert database.db_delete_refund_cleanup_purchase("ORDER_A", True) is True
    assert database.db_get_purchases() == []
    assert database.db_delete_refund_cleanup_purchase("ORDER_A", False) is True
    assert database.db_delete_refund_cleanup_purchase(" ORDER_A", True) is False


def test_host_cleanup_primitive_fails_closed_on_duplicate_rows(isolated_host_db):
    database.db_append_purchase(
        {"buff_order_id": "ORDER_A", "pending_receipt": True, "assetid": None}
    )
    engine = database.get_engine()
    with engine.connect() as connection:
        connection.execute(text("DROP INDEX ux_purchase_buff_order_id"))
        connection.execute(
            text(
                "INSERT INTO purchase "
                "(name, goods_id, price, at, pending_receipt, buff_order_id) "
                "VALUES ('copy', 0, 0, 0, 1, 'ORDER_A')"
            )
        )
        connection.commit()
    assert database.db_delete_refund_cleanup_purchase("ORDER_A", True) is False
    assert len(database.db_get_purchases()) == 2
