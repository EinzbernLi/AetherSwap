from dataclasses import replace
import sqlite3

import pytest

from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.host_ownership import (
    HostPurchaseMutationBlockedError,
    HostPurchaseOwnership,
    classify_host_purchase,
    require_broad_transaction_mutation_allowed,
    require_purchase_mutation_allowed,
)
from app.auto_offer.store import AutoOfferStore, AutoOfferStoreSchemaError, StoredDelivery


def _snapshot(status=DeliveryStatus.PENDING_DIRECTION, **changes):
    value = DeliverySnapshot(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id="account-1",
        recipient_steam_id="76561198000000001",
        delivery_mode=None,
        delivery_status=status,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id=None,
    )
    return replace(value, **changes)


def _stored(status=DeliveryStatus.PENDING_DIRECTION, **changes):
    return StoredDelivery(_snapshot(status, **changes), 1)


def _host(**changes):
    value = {
        "_db_id": 7,
        "buff_order_id": "order-1",
        "goods_id": 123,
        "pending_receipt": True,
        "assetid": None,
        "listing": False,
    }
    value.update(changes)
    return value


def test_missing_store_is_unowned_and_does_not_create_file(tmp_path):
    path = tmp_path / "missing" / "auto_offer.db"
    decision = classify_host_purchase(_host(), store_path=path)
    assert decision.ownership is HostPurchaseOwnership.UNOWNED
    assert not path.exists()
    assert not path.parent.exists()


def test_readonly_inspection_sees_committed_wal_state(tmp_path):
    path = tmp_path / "auto_offer.db"
    store = AutoOfferStore(path)
    store.initialize()
    expected = store.ensure_initial(_snapshot())

    assert AutoOfferStore.inspect_existing_by_buff_order_id(path, "order-1") == expected
    assert AutoOfferStore.inspect_existing(path) == [expected]
    assert store.get_by_buff_order_id("order-1") == expected
    store.close()


def test_readonly_inspection_never_migrates_existing_v1_file(tmp_path):
    path = tmp_path / "auto_offer.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    before = path.read_bytes()
    with pytest.raises(AutoOfferStoreSchemaError):
        AutoOfferStore.inspect_existing(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.OFFER_TERMINATED,
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryStatus.BLOCKED,
    ],
)
def test_nonreleased_store_rows_are_protected(status):
    decision = classify_host_purchase(
        _host(),
        store_index={"order-1": _stored(status)},
    )
    assert decision.ownership is HostPurchaseOwnership.MANAGED
    with pytest.raises(HostPurchaseMutationBlockedError) as exc_info:
        require_purchase_mutation_allowed(
            _host(),
            operation="update",
            data={"assetid": "manual"},
            store_index={"order-1": _stored(status)},
        )
    assert exc_info.value.code == "AUTO_OFFER_PURCHASE_MANAGED"


def test_received_exact_handoff_remains_protected_until_host_receipt():
    received = _stored(
        DeliveryStatus.RECEIVED,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        received_at=10.0,
        pending_receipt=False,
        assetid="asset-1",
        counterparty_steam_id="76561198000000002",
    )
    decision = classify_host_purchase(
        _host(),
        store_index={"order-1": received},
    )
    assert decision.ownership is HostPurchaseOwnership.RECEIPT_PENDING


def test_received_exact_completed_host_receipt_is_released():
    received = _stored(
        DeliveryStatus.RECEIVED,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        received_at=10.0,
        pending_receipt=False,
        assetid="asset-1",
        counterparty_steam_id="76561198000000002",
    )
    decision = classify_host_purchase(
        _host(pending_receipt=False, assetid="asset-1"),
        store_index={"order-1": received},
    )
    assert decision.ownership is HostPurchaseOwnership.RELEASED


@pytest.mark.parametrize(
    "host_changes",
    [
        {"pending_receipt": False, "assetid": None},
        {"pending_receipt": False, "assetid": "wrong-asset"},
        {"pending_receipt": True, "assetid": "asset-1"},
    ],
)
def test_received_host_contradictions_fail_closed(host_changes):
    received = _stored(
        DeliveryStatus.RECEIVED,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        received_at=10.0,
        pending_receipt=False,
        assetid="asset-1",
        counterparty_steam_id="76561198000000002",
    )
    decision = classify_host_purchase(
        _host(**host_changes),
        store_index={"order-1": received},
    )
    assert decision.ownership is HostPurchaseOwnership.UNSAFE


@pytest.mark.parametrize("status", [DeliveryStatus.CANCELLED, DeliveryStatus.REFUNDED])
def test_terminal_tombstone_with_existing_host_purchase_is_unsafe(status):
    decision = classify_host_purchase(
        _host(),
        store_index={"order-1": _stored(status)},
    )
    assert decision.ownership is HostPurchaseOwnership.UNSAFE


def test_missing_order_or_missing_store_row_preserves_legacy_behavior():
    assert classify_host_purchase(
        {"name": "legacy"},
        store_index={},
    ).ownership is HostPurchaseOwnership.UNOWNED
    assert classify_host_purchase(
        _host(),
        store_index={},
    ).ownership is HostPurchaseOwnership.UNOWNED
    require_purchase_mutation_allowed(
        _host(),
        operation="update",
        data={"goods_id": 999},
        store_index={},
    )


def test_released_rows_freeze_delivery_identity_but_allow_commercial_fields():
    received = _stored(
        DeliveryStatus.RECEIVED,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        received_at=10.0,
        pending_receipt=False,
        assetid="asset-1",
        counterparty_steam_id="76561198000000002",
    )
    index = {"order-1": received}
    host = _host(pending_receipt=False, assetid="asset-1")

    require_purchase_mutation_allowed(
        host,
        operation="update",
        data={"listing": True, "sale_price": 12.3},
        store_index=index,
    )
    for field, value in (
        ("buff_order_id", "other"),
        ("goods_id", 999),
        ("pending_receipt", True),
        ("assetid", "other"),
    ):
        with pytest.raises(HostPurchaseMutationBlockedError) as exc_info:
            require_purchase_mutation_allowed(
                host,
                operation="update",
                data={field: value},
                store_index=index,
            )
        assert exc_info.value.code == "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE"


def test_broad_clear_blocks_managed_purchase(monkeypatch):
    managed = _stored()
    monkeypatch.setattr(
        "app.auto_offer.host_ownership._store_index",
        lambda _path=None: {"order-1": managed},
    )
    with pytest.raises(HostPurchaseMutationBlockedError) as exc_info:
        require_broad_transaction_mutation_allowed([_host()])
    assert exc_info.value.code == "AUTO_OFFER_PURCHASE_MANAGED"


def test_broad_replace_cannot_rewrite_released_delivery_identity(monkeypatch):
    received = _stored(
        DeliveryStatus.RECEIVED,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        received_at=10.0,
        pending_receipt=False,
        assetid="asset-1",
        counterparty_steam_id="76561198000000002",
    )
    monkeypatch.setattr(
        "app.auto_offer.host_ownership._store_index",
        lambda _path=None: {"order-1": received},
    )
    current = _host(pending_receipt=False, assetid="asset-1")
    proposed = dict(current, goods_id=999)
    with pytest.raises(HostPurchaseMutationBlockedError) as exc_info:
        require_broad_transaction_mutation_allowed(
            [current],
            proposed_purchases=[proposed],
        )
    assert exc_info.value.code in {
        "AUTO_OFFER_OWNERSHIP_UNSAFE",
        "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE",
    }
