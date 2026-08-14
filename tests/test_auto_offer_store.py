import sqlite3
from dataclasses import replace

import pytest

from app.auto_offer.contracts import DeliveryContractError, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import (
    AUTO_OFFER_STORE_SCHEMA_VERSION,
    AutoOfferStore,
    AutoOfferStoreConflictError,
    AutoOfferStoreCorruptError,
    AutoOfferStoreError,
    AutoOfferStoreSchemaError,
    AutoOfferStoreStaleWriteError,
    StoredDelivery,
)


def snapshot(**changes):
    value = DeliverySnapshot(
        purchase_id="purchase-1",
        buff_order_id="buff-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
        delivery_mode=None,
        delivery_status=DeliveryStatus.PENDING_DIRECTION,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
    )
    return replace(value, **changes)


def make_store(tmp_path):
    store = AutoOfferStore(tmp_path / "auto_offer.db")
    store.initialize()
    return store


def test_constructor_and_import_are_side_effect_free(tmp_path):
    path = tmp_path / "nested" / "auto_offer.db"
    store = AutoOfferStore(path)
    assert not path.exists()
    assert not path.parent.exists()
    assert not hasattr(store, "force_status")
    assert not hasattr(store, "raw_execute")


def test_initialize_creates_schema_and_pragmas(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "auto_offer.db"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == AUTO_OFFER_STORE_SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        columns = connection.execute("PRAGMA table_info(auto_offer_delivery)").fetchall()
        assert [column[1] for column in columns] == [
            "id", "purchase_id", "buff_order_id", "account_id", "recipient_steam_id",
            "delivery_mode", "delivery_status", "steam_tradeoffer_id",
            "offer_attempted_at", "offer_sent_at", "received_at", "delivery_error",
            "pending_receipt", "assetid", "revision", "counterparty_steam_id",
        ]
    store.close()


def test_operations_require_explicit_initialize(tmp_path):
    store = AutoOfferStore(tmp_path / "auto_offer.db")
    with pytest.raises(AutoOfferStoreError):
        store.get_by_purchase_id("purchase-1")
    assert not (tmp_path / "auto_offer.db").exists()


def test_initial_persists_and_reload_preserves_snapshot_and_revision(tmp_path):
    value = snapshot()
    first = make_store(tmp_path)
    stored = first.ensure_initial(value)
    first.close()
    second = make_store(tmp_path)
    assert stored == StoredDelivery(value, 1)
    assert second.get_by_purchase_id("purchase-1") == stored
    assert second.get_by_buff_order_id("buff-1") == stored


def test_v1_migration_is_atomic_and_preserves_historical_null_counterparty(tmp_path):
    path = tmp_path / "auto_offer.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE auto_offer_delivery ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "purchase_id TEXT NOT NULL UNIQUE, buff_order_id TEXT NOT NULL UNIQUE, "
            "account_id TEXT NOT NULL, recipient_steam_id TEXT NOT NULL, "
            "delivery_mode TEXT NULL, delivery_status TEXT NOT NULL, "
            "steam_tradeoffer_id TEXT NULL, offer_attempted_at REAL NULL, "
            "offer_sent_at REAL NULL, received_at REAL NULL, delivery_error TEXT NULL, "
            "pending_receipt INTEGER NOT NULL, assetid TEXT NULL, revision INTEGER NOT NULL"
            ")"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO auto_offer_delivery ("
            "purchase_id, buff_order_id, account_id, recipient_steam_id, "
            "delivery_mode, delivery_status, steam_tradeoffer_id, offer_attempted_at, "
            "offer_sent_at, received_at, delivery_error, pending_receipt, assetid, revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "purchase-1",
                "buff-1",
                "account-1",
                "steam-1",
                DeliveryMode.SELLER_SENDS_OFFER.value,
                DeliveryStatus.AWAITING_OFFER.value,
                None,
                None,
                None,
                None,
                None,
                1,
                None,
                4,
            ),
        )
        connection.commit()

    store = AutoOfferStore(path)
    store.initialize()
    migrated = store.get_by_purchase_id("purchase-1")

    assert migrated == StoredDelivery(
        snapshot(
            delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
            delivery_status=DeliveryStatus.AWAITING_OFFER,
            counterparty_steam_id=None,
        ),
        4,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT counterparty_steam_id FROM auto_offer_delivery"
        ).fetchone() == (None,)


def test_ensure_initial_is_exactly_idempotent(tmp_path):
    store = make_store(tmp_path)
    value = snapshot()
    first = store.ensure_initial(value)
    second = store.ensure_initial(value)
    assert second == first
    assert store._connection.execute("SELECT COUNT(*), MAX(revision) FROM auto_offer_delivery").fetchone() == (1, 1)


def test_identity_conflicts_fail_closed(tmp_path):
    store = make_store(tmp_path)
    store.ensure_initial(snapshot())
    with pytest.raises(AutoOfferStoreConflictError):
        store.ensure_initial(snapshot(account_id="another-account"))
    with pytest.raises(AutoOfferStoreConflictError):
        store.ensure_initial(snapshot(purchase_id="purchase-2"))


def test_initial_requires_pending_direction(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(DeliveryContractError):
        store.ensure_initial(snapshot(delivery_status=DeliveryStatus.AWAITING_OFFER, delivery_mode=DeliveryMode.BUYER_SENDS_OFFER))


def test_buyer_transition_and_optimistic_revision(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
    )
    initial = store.advance(initial, awaiting)
    attempted = snapshot(
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
        offer_attempted_at=10.0,
    )
    attempted_stored = store.advance(initial, attempted)
    assert attempted_stored.revision == 3
    sent = replace(
        attempted,
        delivery_status=DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_sent_at=11.0,
    )
    assert store.advance(attempted_stored, sent).revision == 4


def test_seller_transition_is_supported(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        counterparty_steam_id="seller-1",
    )
    initial = store.advance(initial, awaiting)
    received = snapshot(
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.OFFER_RECEIVED,
        steam_tradeoffer_id="offer-2",
        counterparty_steam_id="seller-1",
    )
    assert store.advance(initial, received).snapshot == received


def test_invalid_transition_and_result_unknown_resend_are_rejected(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    with pytest.raises(DeliveryContractError):
        store.advance(initial, snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.OFFER_SENT, steam_tradeoffer_id="offer-1", offer_attempted_at=1.0, offer_sent_at=2.0))

    arbitrary_unknown = snapshot(
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    with pytest.raises(DeliveryContractError, match="durable write-attempt"):
        store.advance(initial, arbitrary_unknown)

    awaiting = snapshot(
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
    )
    current = store.advance(initial, awaiting)
    attempted = replace(
        awaiting,
        delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
        offer_attempted_at=3.0,
    )
    attempted_stored = store.advance(current, attempted)
    unknown = replace(
        attempted,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    unknown_stored = store.advance(attempted_stored, unknown)
    with pytest.raises(DeliveryContractError, match="write attempt"):
        store.advance(
            unknown_stored,
            replace(
                unknown,
                delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
                delivery_error=None,
            ),
        )


def test_stale_revision_never_overwrites_new_state(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.AWAITING_OFFER)
    current = store.advance(initial, awaiting)
    with pytest.raises(AutoOfferStoreStaleWriteError):
        store.advance(initial, awaiting)
    assert store.get_by_purchase_id("purchase-1") == current


def test_identity_is_immutable_on_advance(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    target = snapshot(buff_order_id="different-buff", delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.AWAITING_OFFER)
    with pytest.raises(DeliveryContractError):
        store.advance(initial, target)
    assert store.get_by_purchase_id("purchase-1") == initial


def test_recoverable_excludes_terminal_rows_and_is_ordered(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    second_value = snapshot(purchase_id="purchase-2", buff_order_id="buff-2")
    store.ensure_initial(second_value)
    awaiting = snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.AWAITING_OFFER)
    current = store.advance(initial, awaiting)
    attempted = snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.OFFER_ATTEMPTED, offer_attempted_at=1.0)
    store.advance(current, attempted)
    assert [item.snapshot.purchase_id for item in store.list_recoverable()] == ["purchase-1", "purchase-2"]

    received = replace(attempted, delivery_status=DeliveryStatus.OFFER_SENT, steam_tradeoffer_id="offer-1", offer_sent_at=2.0)
    confirmed = replace(received, delivery_status=DeliveryStatus.OFFER_CONFIRMED)
    awaiting = replace(confirmed, delivery_status=DeliveryStatus.AWAITING_INVENTORY)
    done = replace(awaiting, delivery_status=DeliveryStatus.RECEIVED, pending_receipt=False, received_at=3.0, assetid="asset-1")
    current = store.get_by_purchase_id("purchase-1")
    current = store.advance(current, received)
    current = store.advance(current, confirmed)
    current = store.advance(current, awaiting)
    store.advance(current, done)
    assert [item.snapshot.purchase_id for item in store.list_recoverable()] == ["purchase-2"]


def test_accept_attempted_and_offer_terminated_survive_restart_recovery(tmp_path):
    store = make_store(tmp_path)

    attempted_initial = store.ensure_initial(snapshot())
    attempted_awaiting = replace(
        attempted_initial.snapshot,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        counterparty_steam_id="seller-1",
    )
    attempted = store.advance(attempted_initial, attempted_awaiting)
    attempted = store.advance(
        attempted,
        replace(
            attempted_awaiting,
            delivery_status=DeliveryStatus.OFFER_RECEIVED,
            steam_tradeoffer_id="offer-1",
        ),
    )
    attempted = store.advance(
        attempted,
        replace(attempted.snapshot, delivery_status=DeliveryStatus.OFFER_CONFIRMED),
    )
    attempted = store.advance(
        attempted,
        replace(
            attempted.snapshot,
            delivery_status=DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        ),
    )

    terminated_initial = store.ensure_initial(
        snapshot(purchase_id="purchase-2", buff_order_id="buff-2")
    )
    terminated_awaiting = replace(
        terminated_initial.snapshot,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        counterparty_steam_id="seller-2",
    )
    terminated = store.advance(terminated_initial, terminated_awaiting)
    terminated = store.advance(
        terminated,
        replace(
            terminated_awaiting,
            delivery_status=DeliveryStatus.OFFER_RECEIVED,
            steam_tradeoffer_id="offer-2",
        ),
    )
    terminated = store.advance(
        terminated,
        replace(
            terminated.snapshot,
            delivery_status=DeliveryStatus.OFFER_TERMINATED,
            delivery_error="offer_terminated",
        ),
    )
    store.close()

    reopened = make_store(tmp_path)
    recovered = reopened.list_recoverable()
    assert recovered == [attempted, terminated]


def test_bound_counterparty_survives_restart_and_cannot_change_or_clear(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        counterparty_steam_id="seller-1",
    )
    current = store.advance(initial, awaiting)
    store.close()

    reopened = make_store(tmp_path)
    current = reopened.get_by_purchase_id("purchase-1")
    assert current.snapshot.counterparty_steam_id == "seller-1"

    for replacement in (None, "seller-2"):
        with pytest.raises(
            DeliveryContractError,
            match="bound counterparty Steam ID cannot change",
        ):
            reopened.advance(
                current,
                replace(
                    current.snapshot,
                    delivery_status=DeliveryStatus.OFFER_RECEIVED,
                    steam_tradeoffer_id="offer-1",
                    counterparty_steam_id=replacement,
                ),
            )
        assert reopened.get_by_purchase_id("purchase-1") == current


def test_historical_null_counterparty_cannot_be_adopted_after_direction_binding(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
    )
    current = store.advance(initial, awaiting)

    with pytest.raises(
        DeliveryContractError,
        match="counterparty Steam ID cannot be adopted after direction binding",
    ):
        store.advance(
            current,
            replace(
                awaiting,
                delivery_status=DeliveryStatus.OFFER_RECEIVED,
                steam_tradeoffer_id="offer-1",
                counterparty_steam_id="seller-guessed",
            ),
        )
    assert store.get_by_purchase_id("purchase-1") == current


def test_corrupt_rows_fail_closed(tmp_path):
    store = make_store(tmp_path)
    store.ensure_initial(snapshot())
    path = tmp_path / "auto_offer.db"
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE auto_offer_delivery SET delivery_status = 'not-a-status'")
        connection.commit()
    reopened = make_store(tmp_path)
    with pytest.raises(AutoOfferStoreCorruptError):
        reopened.get_by_purchase_id("purchase-1")


@pytest.mark.parametrize("column,value", [("pending_receipt", 2), ("revision", 0)])
def test_invalid_storage_scalars_fail_closed(tmp_path, column, value):
    store = make_store(tmp_path)
    store.ensure_initial(snapshot())
    path = tmp_path / "auto_offer.db"
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE auto_offer_delivery SET {column} = ?", (value,))
        connection.commit()
    reopened = make_store(tmp_path)
    with pytest.raises(AutoOfferStoreCorruptError):
        reopened.list_recoverable()


def test_schema_version_and_shape_fail_closed(tmp_path):
    path = tmp_path / "auto_offer.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    with pytest.raises(AutoOfferStoreSchemaError):
        AutoOfferStore(path).initialize()

    path2 = tmp_path / "zero-with-table.db"
    connection = sqlite3.connect(path2)
    connection.execute("CREATE TABLE auto_offer_delivery (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(AutoOfferStoreSchemaError):
        AutoOfferStore(path2).initialize()


def test_write_failure_rolls_back_old_row(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.AWAITING_OFFER)
    current = store.advance(initial, awaiting)
    connection = store._connection
    connection.execute(
        "CREATE TRIGGER fail_transition BEFORE UPDATE ON auto_offer_delivery "
        "BEGIN SELECT RAISE(ABORT, 'intentional test failure'); END"
    )
    target = snapshot(delivery_mode=DeliveryMode.BUYER_SENDS_OFFER, delivery_status=DeliveryStatus.OFFER_ATTEMPTED, offer_attempted_at=1.0)
    with pytest.raises(AutoOfferStoreCorruptError):
        store.advance(current, target)
    assert store.get_by_purchase_id("purchase-1") == current


def test_store_does_not_expose_bypass_write_apis():
    names = dir(AutoOfferStore)
    assert not any(name in names for name in ("force_status", "force_update", "update_any", "raw_execute", "generic_upsert"))


def test_bound_tradeoffer_id_cannot_rebind_or_clear_and_row_stays_unchanged(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    awaiting = snapshot(
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        counterparty_steam_id="seller-1",
    )
    current = store.advance(initial, awaiting)
    received = replace(
        awaiting,
        delivery_status=DeliveryStatus.OFFER_RECEIVED,
        steam_tradeoffer_id="offer-1",
    )
    current = store.advance(current, received)

    with pytest.raises(DeliveryContractError, match="bound steam trade offer ID cannot change"):
        store.advance(
            current,
            replace(
                received,
                delivery_status=DeliveryStatus.OFFER_CONFIRMED,
                steam_tradeoffer_id="offer-2",
            ),
        )
    assert store.get_by_purchase_id("purchase-1") == current

    with pytest.raises(DeliveryContractError, match="bound steam trade offer ID cannot change"):
        store.advance(
            current,
            replace(
                received,
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                steam_tradeoffer_id=None,
                delivery_error="write_result_unknown",
            ),
        )
    assert store.get_by_purchase_id("purchase-1") == current

    confirmed = replace(
        received,
        delivery_status=DeliveryStatus.OFFER_CONFIRMED,
    )
    advanced = store.advance(current, confirmed)
    assert advanced.revision == current.revision + 1
    assert advanced.snapshot.steam_tradeoffer_id == "offer-1"


def test_historical_result_unknown_first_binding_remains_supported_in_store(tmp_path):
    store = make_store(tmp_path)
    initial = store.ensure_initial(snapshot())
    store._connection.execute(
        "UPDATE auto_offer_delivery SET delivery_mode = ?, delivery_status = ?, "
        "delivery_error = ?, revision = ? WHERE purchase_id = ?",
        (
            DeliveryMode.BUYER_SENDS_OFFER.value,
            DeliveryStatus.RESULT_UNKNOWN.value,
            "write_result_unknown",
            initial.revision + 1,
            initial.snapshot.purchase_id,
        ),
    )
    store._connection.commit()

    unknown_stored = store.get_by_purchase_id("purchase-1")
    assert unknown_stored.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert unknown_stored.snapshot.steam_tradeoffer_id is None

    sent = replace(
        unknown_stored.snapshot,
        delivery_status=DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_attempted_at=1.0,
        offer_sent_at=2.0,
        delivery_error=None,
    )
    advanced = store.advance(unknown_stored, sent)
    assert advanced.snapshot.steam_tradeoffer_id == "offer-1"
    assert advanced.revision == unknown_stored.revision + 1
