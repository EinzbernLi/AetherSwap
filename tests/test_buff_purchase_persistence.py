from __future__ import annotations

import pytest

import app.auto_offer.host_integration as host_integration
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import StoredDelivery


def _isolated_database(monkeypatch, tmp_path):
    from app import database

    old_engine = database._engine
    if old_engine is not None:
        old_engine.dispose()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)
    database.init_db()
    return database


def _append_pending(database, order_id: str, *, assetid=None, pending_receipt=True):
    database.db_append_purchase(
        {
            "name": f"Item {order_id}",
            "goods_id": 42,
            "price": 10.0,
            "at": 123.0,
            "pending_receipt": pending_receipt,
            "assetid": assetid,
            "buff_order_id": order_id,
        }
    )
    return database.db_get_purchases()[-1]


def test_purchase_round_trip_keeps_buff_reconciliation_ids(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    database.db_append_purchase(
        {
            "name": "Test Item",
            "goods_id": 42,
            "price": 10.0,
            "at": 123.0,
            "pending_receipt": True,
            "buff_order_id": "bill-1",
            "buff_sell_order_id": "sell-1",
            "batch_id": "batch-1",
            "bill_order_id": "bill-1",
        }
    )

    [saved] = database.db_get_purchases()
    assert saved["buff_order_id"] == "bill-1"
    assert saved["buff_sell_order_id"] == "sell-1"
    assert saved["batch_id"] == "batch-1"
    assert saved["bill_order_id"] == "bill-1"

    database.get_engine().dispose()


def test_exact_receipt_completion_is_atomic_and_idempotent(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    saved = _append_pending(database, "bill-1")
    db_id = saved["_db_id"]

    assert database.db_complete_purchase_receipt_by_id(
        db_id, "bill-1", "asset-1"
    ) is True
    [completed] = database.db_get_purchases()
    assert completed["_db_id"] == db_id
    assert completed["buff_order_id"] == "bill-1"
    assert completed["assetid"] == "asset-1"
    assert completed["pending_receipt"] is False
    assert completed["name"] == "Item bill-1"
    assert completed["price"] == 10.0

    assert database.db_complete_purchase_receipt_by_id(
        db_id, "bill-1", "asset-1"
    ) is True
    [replayed] = database.db_get_purchases()
    assert replayed == completed
    database.get_engine().dispose()


def test_receipt_completion_rejects_wrong_identity_without_mutation(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    saved = _append_pending(database, "bill-1")
    db_id = saved["_db_id"]
    before = database.db_get_purchases()

    assert database.db_complete_purchase_receipt_by_id(
        db_id + 100, "bill-1", "asset-1"
    ) is False
    assert database.db_complete_purchase_receipt_by_id(
        db_id, "bill-other", "asset-1"
    ) is False
    assert database.db_get_purchases() == before
    database.get_engine().dispose()


def test_receipt_completion_rejects_incompatible_existing_receipt(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    saved = _append_pending(
        database,
        "bill-1",
        assetid="asset-old",
        pending_receipt=False,
    )
    db_id = saved["_db_id"]
    before = database.db_get_purchases()

    assert database.db_complete_purchase_receipt_by_id(
        db_id, "bill-1", "asset-new"
    ) is False
    assert database.db_get_purchases() == before
    database.get_engine().dispose()


def test_receipt_completion_rejects_asset_owned_by_other_purchase(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    target = _append_pending(database, "bill-1")
    _append_pending(
        database,
        "bill-2",
        assetid="asset-shared",
        pending_receipt=False,
    )
    before = database.db_get_purchases()

    assert database.db_complete_purchase_receipt_by_id(
        target["_db_id"], "bill-1", "asset-shared"
    ) is False
    assert database.db_get_purchases() == before
    database.get_engine().dispose()


def test_receipt_completion_rejects_invalid_inputs(monkeypatch, tmp_path):
    database = _isolated_database(monkeypatch, tmp_path)
    saved = _append_pending(database, "bill-1")
    db_id = saved["_db_id"]
    before = database.db_get_purchases()

    invalid_calls = (
        (True, "bill-1", "asset-1"),
        (0, "bill-1", "asset-1"),
        (-1, "bill-1", "asset-1"),
        (db_id, "", "asset-1"),
        (db_id, " bill-1", "asset-1"),
        (db_id, "bill-1 ", "asset-1"),
        (db_id, "bill-1", ""),
        (db_id, "bill-1", " asset-1"),
        (db_id, "bill-1", "asset-1 "),
    )
    for args in invalid_calls:
        assert database.db_complete_purchase_receipt_by_id(*args) is False
    assert database.db_get_purchases() == before
    database.get_engine().dispose()


def test_host_receipt_writer_exception_is_fail_closed():
    class Bridge:
        account_id = "account-1"
        recipient_steam_id = "76561198000000001"

    def writer(*_args):
        raise RuntimeError("local write failed")

    integration = host_integration.HostAutoOfferIntegration(
        Bridge(),
        complete_purchase_receipt_by_id=writer,
    )
    received = StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id="buff:bill-1",
            buff_order_id="bill-1",
            account_id="account-1",
            recipient_steam_id="76561198000000001",
            delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
            delivery_status=DeliveryStatus.RECEIVED,
            steam_tradeoffer_id="offer-1",
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=12.0,
            delivery_error=None,
            pending_receipt=False,
            assetid="asset-1",
        ),
        revision=8,
    )

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="host_receipt_write_failed",
    ):
        integration._write_back_received(
            {
                "_db_id": 1,
                "buff_order_id": "bill-1",
                "pending_receipt": True,
                "assetid": None,
            },
            received,
        )
