from __future__ import annotations

import app.auto_offer.runtime_lifecycle as lifecycle
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.runtime_mode import AutoOfferRuntimeMode
from app.auto_offer.store import AutoOfferStoreError, StoredDelivery


def _stored(
    order_id: str,
    status: DeliveryStatus,
    *,
    pending_receipt: bool,
    assetid: str | None = None,
    revision: int = 1,
) -> StoredDelivery:
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id="account",
            recipient_steam_id="76561198000000001",
            delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=pending_receipt,
            assetid=assetid,
        ),
        revision,
    )


def _runtime(monkeypatch, **kwargs):
    monkeypatch.setattr(lifecycle, "canary_metadata_present", lambda: False)
    kwargs.setdefault("reconciliation_checked", True)
    return lifecycle.inspect_effective_runtime(**kwargs)


def test_missing_store_requested_off_is_off_without_creating_source(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "auto_offer.db"
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[],
        store_path=path,
    )
    assert state.mode is AutoOfferRuntimeMode.OFF
    assert not path.exists()
    assert not path.parent.exists()


def test_managed_and_receipt_pending_are_draining_when_requested_off(monkeypatch):
    managed = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {
                "_db_id": 1,
                "buff_order_id": "order-1",
                "pending_receipt": True,
                "assetid": None,
            }
        ],
        store_rows=[
            _stored(
                "order-1",
                DeliveryStatus.PENDING_DIRECTION,
                pending_receipt=True,
            )
        ],
    )
    receipt_pending = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {
                "_db_id": 1,
                "buff_order_id": "order-1",
                "pending_receipt": True,
                "assetid": None,
            }
        ],
        store_rows=[
            _stored(
                "order-1",
                DeliveryStatus.RECEIVED,
                pending_receipt=False,
                assetid="asset-1",
            )
        ],
    )
    assert managed.mode is AutoOfferRuntimeMode.DRAINING
    assert managed.active_delivery_count == 1
    assert receipt_pending.mode is AutoOfferRuntimeMode.DRAINING


def test_released_and_store_only_audit_rows_do_not_count_active(monkeypatch):
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {
                "_db_id": 1,
                "buff_order_id": "released-order",
                "pending_receipt": False,
                "assetid": "asset-1",
            }
        ],
        store_rows=[
            _stored(
                "released-order",
                DeliveryStatus.RECEIVED,
                pending_receipt=False,
                assetid="asset-1",
            ),
            _stored(
                "received-audit",
                DeliveryStatus.RECEIVED,
                pending_receipt=False,
                assetid="asset-2",
            ),
            _stored(
                "cancelled-audit",
                DeliveryStatus.CANCELLED,
                pending_receipt=True,
            ),
            _stored(
                "refunded-audit",
                DeliveryStatus.REFUNDED,
                pending_receipt=True,
            ),
        ],
    )
    assert state.mode is AutoOfferRuntimeMode.OFF
    assert state.active_delivery_count == 0


def test_protected_orphan_store_blocks(monkeypatch):
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[],
        store_rows=[
            _stored(
                "orphan-order",
                DeliveryStatus.AWAITING_OFFER,
                pending_receipt=True,
            )
        ],
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED
    assert state.reason == "active_orphan_store"


def test_legacy_pending_is_blocked_only_for_requested_on(monkeypatch):
    purchase = {
        "_db_id": 1,
        "name": "legacy",
        "pending_receipt": True,
        "assetid": None,
    }
    blocked = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": True}},
        purchases=[purchase],
        store_rows=[],
    )
    off = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[purchase],
        store_rows=[],
    )
    assert blocked.mode is AutoOfferRuntimeMode.BLOCKED
    assert blocked.reason == "legacy_pending_unowned"
    assert off.mode is AutoOfferRuntimeMode.OFF


def test_host_correlated_store_blocked_is_effective_blocker(monkeypatch):
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {
                "_db_id": 1,
                "buff_order_id": "blocked-order",
                "pending_receipt": True,
                "assetid": None,
            }
        ],
        store_rows=[
            _stored(
                "blocked-order",
                DeliveryStatus.BLOCKED,
                pending_receipt=True,
            )
        ],
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED
    assert state.reason == "delivery_blocked"
    assert state.active_delivery_count == 1


def test_tombstone_with_existing_host_row_is_unsafe(monkeypatch):
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {
                "_db_id": 1,
                "buff_order_id": "order-1",
                "pending_receipt": True,
                "assetid": None,
            }
        ],
        store_rows=[
            _stored(
                "order-1",
                DeliveryStatus.CANCELLED,
                pending_receipt=True,
            )
        ],
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED
    assert state.reason == "host_store_ownership_unsafe"


def test_duplicate_host_order_identity_blocks(monkeypatch):
    state = _runtime(
        monkeypatch,
        config={"auto_offer": {"enabled": False}},
        purchases=[
            {"_db_id": 1, "buff_order_id": "same"},
            {"_db_id": 2, "buff_order_id": "same"},
        ],
        store_rows=[],
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED
    assert state.reason == "duplicate_host_order_identity"


def test_canary_and_reconciliation_are_outer_blockers(monkeypatch):
    monkeypatch.setattr(lifecycle, "canary_metadata_present", lambda: True)
    canary = lifecycle.inspect_effective_runtime(
        config={"auto_offer": {"enabled": False}},
        purchases=[],
        store_rows=[],
        reconciliation_checked=True,
    )
    monkeypatch.setattr(lifecycle, "canary_metadata_present", lambda: False)
    reconciliation = lifecycle.inspect_effective_runtime(
        config={"auto_offer": {"enabled": True}},
        purchases=[],
        store_rows=[],
        unresolved_checkout={"unresolved": True},
        pipeline_active=False,
    )
    live_pipeline = lifecycle.inspect_effective_runtime(
        config={"auto_offer": {"enabled": True}},
        purchases=[],
        store_rows=[],
        unresolved_checkout={"unresolved": True},
        pipeline_active=True,
    )
    assert canary.mode is AutoOfferRuntimeMode.BLOCKED
    assert canary.reason == "canary_fenced"
    assert reconciliation.reason == "buff_reconciliation_required"
    assert live_pipeline.mode is AutoOfferRuntimeMode.ON


def test_corrupt_store_fails_closed(monkeypatch):
    def corrupt(_path):
        raise AutoOfferStoreError("corrupt")

    monkeypatch.setattr(lifecycle.AutoOfferStore, "inspect_existing", corrupt)
    state = lifecycle.inspect_effective_runtime(
        config={"auto_offer": {"enabled": False}},
        purchases=[],
        reconciliation_checked=True,
    )
    assert state.mode is AutoOfferRuntimeMode.BLOCKED


def test_backend_status_uses_sanitized_effective_runtime_payload(monkeypatch):
    from app.routes import status

    monkeypatch.setattr(status, "get_status", lambda: {"status": "idle"})
    monkeypatch.setattr(status, "get_buff", lambda: {"cookies": ""})
    state = lifecycle.resolve_effective_runtime(
        config={"auto_offer": {"enabled": False}},
        purchases=[],
        store_rows=[],
        reconciliation_checked=True,
    )
    monkeypatch.setattr(status, "get_effective_runtime_state", lambda: state)
    payload = status.api_status()
    assert payload["auto_offer_runtime"] == {
        "requested_enabled": False,
        "mode": "off",
        "active_delivery_count": 0,
        "reason": None,
    }
    assert payload["buff_no_cookie"] is True
