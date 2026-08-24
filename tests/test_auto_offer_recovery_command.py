from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.auto_offer.recovery_command as command
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.store import AutoOfferStore, StoredDelivery


COMMIT = "a" * 40
TREE = "b" * 40
ACCOUNT = "account-1"
RECIPIENT = "76561198000000001"
ORDER = "order-1"
PURCHASE = f"buff:{ORDER}"


def _make_host(path: Path, rows=((7, ORDER, 1, None),)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE purchase ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "buff_order_id TEXT, pending_receipt INTEGER, assetid TEXT"
            ")"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO purchase (id, buff_order_id, pending_receipt, assetid) "
                "VALUES (?, ?, ?, ?)",
                row,
            )
        connection.commit()


def _insert_store_row(
    path: Path,
    *,
    order_id: str = ORDER,
    status: DeliveryStatus = DeliveryStatus.RESULT_UNKNOWN,
    revision: int = 4,
) -> None:
    store = AutoOfferStore(path)
    store.initialize()
    connection = store._connection
    assert connection is not None
    mode = DeliveryMode.BUYER_SENDS_OFFER.value
    attempted = 10.0 if status is DeliveryStatus.RESULT_UNKNOWN else None
    error = "write_result_unknown" if status is DeliveryStatus.RESULT_UNKNOWN else None
    connection.execute(
        "INSERT INTO auto_offer_delivery ("
        "purchase_id, buff_order_id, account_id, recipient_steam_id, "
        "delivery_mode, delivery_status, steam_tradeoffer_id, "
        "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
        "pending_receipt, assetid, counterparty_steam_id, revision"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"buff:{order_id}",
            order_id,
            ACCOUNT,
            RECIPIENT,
            mode,
            status.value,
            None,
            attempted,
            None,
            None,
            error,
            1,
            None,
            None,
            revision,
        ),
    )
    store.close()


def _patch_local_identity(monkeypatch, *, buff_generation=3) -> None:
    monkeypatch.setattr(command, "get_current_id", lambda: ACCOUNT)
    monkeypatch.setattr(
        command,
        "get_account",
        lambda account_id: {"id": account_id, "steam_id": RECIPIENT},
    )
    values = {
        ("steam", "steam_id"): RECIPIENT,
        ("steam", "cookies"): "steam-cookie",
        ("buff", "cookies"): "buff-cookie",
        ("buff", "user_agent"): "ua",
        ("buff", "generation"): buff_generation,
    }
    monkeypatch.setattr(
        command,
        "get_credential_value",
        lambda section, key=None, default=None: values.get((section, key), default),
    )


def _initial_stored() -> StoredDelivery:
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE,
            buff_order_id=ORDER,
            account_id=ACCOUNT,
            recipient_steam_id=RECIPIENT,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            steam_tradeoffer_id=None,
            offer_attempted_at=10.0,
            offer_sent_at=None,
            received_at=None,
            delivery_error="write_result_unknown",
            pending_receipt=True,
            assetid=None,
            counterparty_steam_id=None,
        ),
        4,
    )


def _sequence() -> list[StoredDelivery]:
    initial = _initial_stored()
    sent = StoredDelivery(
        replace(
            initial.snapshot,
            delivery_status=DeliveryStatus.OFFER_SENT,
            steam_tradeoffer_id="offer-1",
            counterparty_steam_id="76561198000000002",
            offer_sent_at=11.0,
            delivery_error=None,
        ),
        5,
    )
    confirmed = StoredDelivery(
        replace(sent.snapshot, delivery_status=DeliveryStatus.OFFER_CONFIRMED),
        6,
    )
    awaiting = StoredDelivery(
        replace(confirmed.snapshot, delivery_status=DeliveryStatus.AWAITING_INVENTORY),
        7,
    )
    received = StoredDelivery(
        replace(
            awaiting.snapshot,
            delivery_status=DeliveryStatus.RECEIVED,
            pending_receipt=False,
            assetid="asset-1",
            received_at=12.0,
        ),
        8,
    )
    return [initial, sent, confirmed, awaiting, received]


def _binding(*, fingerprint="f" * 64) -> command.RecoveryTargetBinding:
    return command.RecoveryTargetBinding(
        source_commit=COMMIT,
        source_tree=TREE,
        fingerprint=fingerprint,
        order_id=ORDER,
        host_db_id=7,
        store=_initial_stored(),
        account_id=ACCOUNT,
        recipient_steam_id=RECIPIENT,
        steam_cookie="steam-cookie",
        buff_cookie="buff-cookie",
        buff_user_agent="ua",
        buff_generation=3,
    )


def test_module_import_does_not_start_fastapi_runtime():
    code = (
        "import sys; import app.auto_offer.recovery_command; "
        "assert 'app.api' not in sys.modules; "
        "assert 'app.services.workers' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_preflight_is_deterministic_and_binds_credentials(monkeypatch, tmp_path):
    host_path = tmp_path / "app.db"
    store_path = tmp_path / "auto_offer.db"
    _make_host(host_path)
    _insert_store_row(store_path)
    monkeypatch.setattr(command, "_verify_source", lambda *_args: (COMMIT, TREE))
    _patch_local_identity(monkeypatch, buff_generation=3)

    first = command.collect_recovery_preflight(
        expected_commit=COMMIT,
        expected_tree=TREE,
        host_db_path=host_path,
        store_path=store_path,
    )
    second = command.collect_recovery_preflight(
        expected_commit=COMMIT,
        expected_tree=TREE,
        host_db_path=host_path,
        store_path=store_path,
    )
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert first.store.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN

    _patch_local_identity(monkeypatch, buff_generation=4)
    changed = command.collect_recovery_preflight(
        expected_commit=COMMIT,
        expected_tree=TREE,
        host_db_path=host_path,
        store_path=store_path,
    )
    assert changed.fingerprint != first.fingerprint


def test_preflight_blocks_unrelated_recoverable_store_row(monkeypatch, tmp_path):
    host_path = tmp_path / "app.db"
    store_path = tmp_path / "auto_offer.db"
    _make_host(host_path)
    _insert_store_row(store_path)
    store = AutoOfferStore(store_path)
    store.initialize_existing()
    connection = store._connection
    assert connection is not None
    connection.execute(
        "INSERT INTO auto_offer_delivery ("
        "purchase_id, buff_order_id, account_id, recipient_steam_id, "
        "delivery_mode, delivery_status, steam_tradeoffer_id, "
        "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
        "pending_receipt, assetid, counterparty_steam_id, revision"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "buff:other-order",
            "other-order",
            ACCOUNT,
            RECIPIENT,
            DeliveryMode.SELLER_SENDS_OFFER.value,
            DeliveryStatus.AWAITING_OFFER.value,
            None,
            None,
            None,
            None,
            None,
            1,
            None,
            None,
            1,
        ),
    )
    store.close()
    monkeypatch.setattr(command, "_verify_source", lambda *_args: (COMMIT, TREE))
    _patch_local_identity(monkeypatch)

    with pytest.raises(command.RecoveryCommandError, match="unrelated_recoverable_store_row"):
        command.collect_recovery_preflight(
            expected_commit=COMMIT,
            expected_tree=TREE,
            host_db_path=host_path,
            store_path=store_path,
        )


def test_fingerprint_mismatch_stops_before_buff_or_maintenance(monkeypatch):
    monkeypatch.setattr(
        command,
        "_make_buff_client",
        lambda _binding: (_ for _ in ()).throw(AssertionError("must not construct BUFF")),
    )
    with pytest.raises(command.RecoveryCommandError, match="target_fingerprint_mismatch"):
        command.execute_recovery(
            _binding(fingerprint="f" * 64),
            expected_fingerprint="e" * 64,
        )


def test_buff_client_snapshot_cannot_persist_rotated_credentials(monkeypatch):
    captured = {}

    class FakeBuffClient:
        def __init__(self, cookies, **kwargs):
            captured["cookies"] = cookies
            captured.update(kwargs)

    monkeypatch.setattr(command, "BuffClient", FakeBuffClient)
    client = command._make_buff_client(_binding())
    assert isinstance(client, FakeBuffClient)
    assert captured["cookies"] == "buff-cookie"
    assert captured["credentials_provider"] is None
    assert captured["credentials_update_callback"] is None


def test_execute_advances_one_transition_per_tick_then_one_receipt(monkeypatch):
    states = _sequence()
    cursor = {"index": 0}
    receipt = {"done": False, "calls": 0}

    class FakeBuff:
        def close(self):
            pass

    class FakeMaintenance:
        def run_recovery_tick(self, host_rows):
            assert len(host_rows) == 1
            assert cursor["index"] < len(states) - 1
            cursor["index"] += 1
            result = (
                AutoOfferResult.COMPLETE
                if states[cursor["index"]].snapshot.delivery_status is DeliveryStatus.RECEIVED
                else AutoOfferResult.WAITING
            )
            return SimpleNamespace(result=result)

        def complete_host_receipt(self, host_rows):
            assert states[cursor["index"]].snapshot.delivery_status is DeliveryStatus.RECEIVED
            receipt["calls"] += 1
            receipt["done"] = True
            return True

        def close(self):
            pass

    def host_rows():
        return [
            {
                "_db_id": 7,
                "buff_order_id": ORDER,
                "pending_receipt": not receipt["done"],
                "assetid": "asset-1" if receipt["done"] else None,
            }
        ]

    monkeypatch.setattr(command, "_make_buff_client", lambda _binding: FakeBuff())
    monkeypatch.setattr(command, "_make_maintenance", lambda _client: FakeMaintenance())
    monkeypatch.setattr(command, "_host_rows", host_rows)
    monkeypatch.setattr(command, "_read_store_target", lambda _order: states[cursor["index"]])

    assert command.execute_recovery(
        _binding(), expected_fingerprint="f" * 64
    ) == 0
    assert cursor["index"] == 4
    assert receipt["calls"] == 1


def test_confirmation_required_stops_without_another_tick_or_receipt(monkeypatch):
    states = _sequence()
    confirmation = StoredDelivery(
        replace(states[1].snapshot, delivery_status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED),
        6,
    )
    states = [states[0], states[1], confirmation]
    cursor = {"index": 0}
    calls = {"ticks": 0, "receipt": 0}

    class FakeBuff:
        def close(self):
            pass

    class FakeMaintenance:
        def run_recovery_tick(self, _host_rows):
            calls["ticks"] += 1
            cursor["index"] += 1
            return SimpleNamespace(result=AutoOfferResult.WAITING)

        def complete_host_receipt(self, _host_rows):
            calls["receipt"] += 1
            return True

        def close(self):
            pass

    monkeypatch.setattr(command, "_make_buff_client", lambda _binding: FakeBuff())
    monkeypatch.setattr(command, "_make_maintenance", lambda _client: FakeMaintenance())
    monkeypatch.setattr(
        command,
        "_host_rows",
        lambda: [{"_db_id": 7, "buff_order_id": ORDER, "pending_receipt": True, "assetid": None}],
    )
    monkeypatch.setattr(command, "_read_store_target", lambda _order: states[cursor["index"]])

    assert command.execute_recovery(
        _binding(), expected_fingerprint="f" * 64
    ) == 2
    assert calls["ticks"] == 2
    assert calls["receipt"] == 0


def test_wait_without_persisted_transition_stops_after_one_tick(monkeypatch):
    state = _initial_stored()
    calls = {"ticks": 0}

    class FakeBuff:
        def close(self):
            pass

    class FakeMaintenance:
        def run_recovery_tick(self, _host_rows):
            calls["ticks"] += 1
            return SimpleNamespace(result=AutoOfferResult.WAITING)

        def close(self):
            pass

    monkeypatch.setattr(command, "_make_buff_client", lambda _binding: FakeBuff())
    monkeypatch.setattr(command, "_make_maintenance", lambda _client: FakeMaintenance())
    monkeypatch.setattr(command, "_host_rows", lambda: [{"_db_id": 7, "buff_order_id": ORDER, "pending_receipt": True, "assetid": None}])
    monkeypatch.setattr(command, "_read_store_target", lambda _order: state)

    assert command.execute_recovery(
        _binding(), expected_fingerprint="f" * 64
    ) == 2
    assert calls["ticks"] == 1
