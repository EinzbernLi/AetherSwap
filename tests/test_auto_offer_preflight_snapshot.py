import sqlite3
from dataclasses import FrozenInstanceError

import pytest

import app.auto_offer.preflight_snapshot as snapshot_module
from app.auto_offer.contracts import DeliveryStatus
from app.auto_offer.preflight_snapshot import (
    PreflightSnapshotError,
    collect_local_preflight_snapshot,
)
from app.auto_offer.store import AutoOfferStore


_STORE_SQL = """
CREATE TABLE auto_offer_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id TEXT NOT NULL UNIQUE,
    buff_order_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    recipient_steam_id TEXT NOT NULL,
    delivery_mode TEXT NULL,
    delivery_status TEXT NOT NULL,
    steam_tradeoffer_id TEXT NULL,
    offer_attempted_at REAL NULL,
    offer_sent_at REAL NULL,
    received_at REAL NULL,
    delivery_error TEXT NULL,
    pending_receipt INTEGER NOT NULL,
    assetid TEXT NULL,
    revision INTEGER NOT NULL,
    counterparty_steam_id TEXT NULL
)
"""


def _make_host(path, rows=(), *, with_secret_columns=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        extra = ", name TEXT, buyer_info TEXT" if with_secret_columns else ""
        connection.execute(
            "CREATE TABLE purchase ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "buff_order_id TEXT, pending_receipt INTEGER, assetid TEXT"
            f"{extra})"
        )
        for row in rows:
            if with_secret_columns:
                connection.execute(
                    "INSERT INTO purchase "
                    "(buff_order_id, pending_receipt, assetid, name, buyer_info) "
                    "VALUES (?, ?, ?, ?, ?)",
                    row,
                )
            else:
                connection.execute(
                    "INSERT INTO purchase "
                    "(buff_order_id, pending_receipt, assetid) VALUES (?, ?, ?)",
                    row,
                )
        connection.commit()


def _make_store(path, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(_STORE_SQL)
        connection.execute("PRAGMA user_version = 2")
        for row in rows:
            connection.execute(
                "INSERT INTO auto_offer_delivery ("
                "purchase_id, buff_order_id, account_id, recipient_steam_id, "
                "delivery_mode, delivery_status, steam_tradeoffer_id, "
                "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
                "pending_receipt, assetid, revision, counterparty_steam_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        connection.commit()


def _initial_store_row(
    purchase_id="buff:order-1",
    buff_order_id="order-1",
    account_id="account-1",
    recipient_steam_id="76561198000000001",
    counterparty_steam_id=None,
):
    return (
        purchase_id,
        buff_order_id,
        account_id,
        recipient_steam_id,
        None,
        DeliveryStatus.PENDING_DIRECTION.value,
        None,
        None,
        None,
        None,
        None,
        1,
        None,
        1,
        counterparty_steam_id,
    )


def _fingerprint(path):
    result = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path if not suffix else type(path)(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            result[suffix] = (candidate.read_bytes(), stat.st_size, stat.st_mtime_ns)
        else:
            result[suffix] = None
    return result


def test_absent_host_fails_without_creating_paths(tmp_path):
    host = tmp_path / "missing" / "app.db"
    store = tmp_path / "other" / "auto_offer.db"

    with pytest.raises(PreflightSnapshotError, match="^host_db_missing$"):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )

    assert not host.exists()
    assert not host.parent.exists()
    assert not store.exists()
    assert not store.parent.exists()


def test_absent_store_is_reported_without_creation(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "nested" / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert result.store_exists is False
    assert result.store_rows == ()
    assert [item.buff_order_id for item in result.host_pending] == ["order-1"]
    assert not store.exists()
    assert not store.parent.exists()


def test_existing_sources_are_byte_and_mtime_unchanged(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])
    before_host = _fingerprint(host)
    before_store = _fingerprint(store)

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert result.store_exists is True
    assert _fingerprint(host) == before_host
    assert _fingerprint(store) == before_store


def test_cleanly_closed_wal_store_is_read_from_detached_copy(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])
    with sqlite3.connect(store) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        connection.commit()
    assert not type(store)(str(store) + "-wal").exists()
    assert not type(store)(str(store) + "-shm").exists()
    before = _fingerprint(store)

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert result.store_rows[0].buff_order_id == "order-1"
    assert _fingerprint(store) == before


def test_sqlite_only_opens_memory_images_not_source_paths(tmp_path, monkeypatch):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])
    real_connect = sqlite3.connect
    opened = []

    def guarded_connect(database, *args, **kwargs):
        opened.append(database)
        assert database == ":memory:"
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", guarded_connect)

    collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert opened == [":memory:", ":memory:"]


def test_existing_store_runtime_mutators_are_not_used(tmp_path, monkeypatch):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])

    def forbidden(*args, **kwargs):
        raise AssertionError("write-capable Store API reached")

    monkeypatch.setattr(AutoOfferStore, "initialize", forbidden)
    monkeypatch.setattr(AutoOfferStore, "ensure_initial", forbidden)
    monkeypatch.setattr(AutoOfferStore, "ensure_initial_with_created", forbidden)
    monkeypatch.setattr(AutoOfferStore, "advance", forbidden)

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert result.store_rows[0].revision == 1


def test_two_host_pending_rows_are_preserved_not_selected(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(
        host,
        [
            ("order-1", 1, None),
            ("order-2", 1, None),
            ("completed", 0, "asset-9"),
        ],
    )

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert [(item.host_db_id, item.buff_order_id) for item in result.host_pending] == [
        (1, "order-1"),
        (2, "order-2"),
    ]


def test_store_rows_are_detached_exact_and_ordered(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-2", 1, None)])
    _make_store(
        store,
        [
            _initial_store_row("buff:order-1", "order-1", "account-1"),
            _initial_store_row("buff:order-2", "order-2", "account-2"),
        ],
    )

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert [row.purchase_id for row in result.store_rows] == [
        "buff:order-1",
        "buff:order-2",
    ]
    assert result.store_rows[1].account_id == "account-2"
    assert result.store_rows[1].delivery_status is DeliveryStatus.PENDING_DIRECTION
    assert result.store_rows[1].pending_receipt is True
    assert result.store_rows[1].counterparty_steam_id is None


def test_store_v2_counterparty_binding_is_preserved_in_detached_evidence(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(
        store,
        [_initial_store_row(counterparty_steam_id="76561198000000002")],
    )

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    assert result.store_rows[0].counterparty_steam_id == "76561198000000002"


def test_existing_store_with_wrong_schema_version_fails_without_repair(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [])
    _make_store(store, [])
    with sqlite3.connect(store) as connection:
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    before = _fingerprint(store)

    with pytest.raises(
        PreflightSnapshotError,
        match="^auto_offer_store_schema_mismatch$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )

    assert _fingerprint(store) == before


def test_existing_uninitialized_store_file_fails_closed(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [])
    with sqlite3.connect(store):
        pass

    with pytest.raises(
        PreflightSnapshotError,
        match="^auto_offer_store_schema_mismatch$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )


def test_host_schema_missing_required_column_fails_closed(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    with sqlite3.connect(host) as connection:
        connection.execute(
            "CREATE TABLE purchase ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "pending_receipt INTEGER, assetid TEXT)"
        )
        connection.commit()

    with pytest.raises(
        PreflightSnapshotError,
        match="^host_purchase_schema_mismatch$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )


def test_duplicate_host_order_identity_fails_closed(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(
        host,
        [
            ("order-1", 1, None),
            ("order-1", 1, None),
        ],
    )

    with pytest.raises(
        PreflightSnapshotError,
        match="^duplicate_host_buff_order_id$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_any_sqlite_sidecar_fails_before_collection(tmp_path, suffix):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [])
    _make_store(store, [])
    sidecar = type(store)(str(store) + suffix)
    sidecar.write_bytes(b"do-not-touch")
    before = _fingerprint(store)

    with pytest.raises(
        PreflightSnapshotError,
        match="^sqlite_source_not_quiescent$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )

    assert _fingerprint(store) == before


def test_source_change_during_collection_is_detected(tmp_path, monkeypatch):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])
    original_collect_store = snapshot_module._collect_store

    def changing_collect_store(payload):
        result = original_collect_store(payload)
        with sqlite3.connect(host) as connection:
            connection.execute(
                "INSERT INTO purchase "
                "(buff_order_id, pending_receipt, assetid) VALUES (?, ?, ?)",
                ("order-2", 1, None),
            )
            connection.commit()
        return result

    monkeypatch.setattr(snapshot_module, "_collect_store", changing_collect_store)

    with pytest.raises(
        PreflightSnapshotError,
        match="^source_changed_during_collection$",
    ):
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=store,
        )


def test_detached_evidence_is_frozen(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    with pytest.raises(FrozenInstanceError):
        result.host_pending[0].buff_order_id = "changed"


def test_secret_shaped_unselected_fields_and_paths_never_enter_evidence_or_errors(tmp_path):
    secret = "steamLoginSecure=TOP-SECRET"
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(
        host,
        [("order-1", 1, None, secret, f'{{"cookie":"{secret}"}}')],
        with_secret_columns=True,
    )

    result = collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )
    assert secret not in repr(result)

    bad_store = tmp_path / f"{secret}.db"
    bad_store.write_bytes(b"not sqlite")
    with pytest.raises(PreflightSnapshotError) as exc_info:
        collect_local_preflight_snapshot(
            host_db_path=host,
            auto_offer_store_path=bad_store,
        )
    assert secret not in str(exc_info.value)


def test_no_source_sidecars_are_created(tmp_path):
    host = tmp_path / "app.db"
    store = tmp_path / "auto_offer.db"
    _make_host(host, [("order-1", 1, None)])
    _make_store(store, [_initial_store_row()])

    collect_local_preflight_snapshot(
        host_db_path=host,
        auto_offer_store_path=store,
    )

    for path in (host, store):
        for suffix in ("-wal", "-shm", "-journal"):
            assert not type(path)(str(path) + suffix).exists()
