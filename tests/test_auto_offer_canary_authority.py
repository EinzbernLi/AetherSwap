from __future__ import annotations

import dataclasses
import json
import pickle
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.auto_offer.canary_authority import (
    CanaryAuthority,
    CanaryAuthorityBusyError,
    CanaryAuthorityError,
    CanaryAuthorityStaleError,
    CanaryPermit,
    CanaryWriteBlockedError,
    CanaryWriteTarget,
)
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.store import StoredDelivery

ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000007"
ORDER_ID = "buff-order-7"
PURCHASE_ID = f"buff:{ORDER_ID}"


def _permit(
    *,
    permit_id: str = "permit-1",
    owner_nonce: str = "owner-1",
    expected_store_present: bool = False,
    expected_store_revision: int | None = None,
    expected_store_status: str | None = None,
    expected_store_tradeoffer_id: str | None = None,
    created_at: float = 123.0,
) -> CanaryPermit:
    return CanaryPermit(
        permit_id=permit_id,
        owner_nonce=owner_nonce,
        host_db_id=7,
        buff_order_id=ORDER_ID,
        purchase_id=PURCHASE_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        expected_host_order_ids=(ORDER_ID,),
        expected_store_present=expected_store_present,
        expected_store_revision=expected_store_revision,
        expected_store_status=expected_store_status,
        expected_store_tradeoffer_id=expected_store_tradeoffer_id,
        created_at=created_at,
    )


def _target(action: str, *, order_id: str = ORDER_ID, db_id: int | None = None, assetid=None):
    return CanaryWriteTarget(
        action=action,
        purchase_id=f"buff:{order_id}",
        buff_order_id=order_id,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        host_db_id=db_id,
        assetid=assetid,
    )


def _host_db(path: Path, rows=((7, ORDER_ID, 1, None),)) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE purchase ("
            "id INTEGER PRIMARY KEY, "
            "buff_order_id TEXT, "
            "pending_receipt INTEGER, "
            "assetid TEXT)"
        )
        connection.executemany(
            "INSERT INTO purchase(id,buff_order_id,pending_receipt,assetid) VALUES(?,?,?,?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _stored(
    status: DeliveryStatus,
    *,
    mode: DeliveryMode | None = DeliveryMode.BUYER_SENDS_OFFER,
    revision: int = 1,
    tradeoffer_id: str | None = None,
) -> StoredDelivery:
    attempted_at = None
    sent_at = None
    if status in {
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    }:
        attempted_at = 1.0
    if status in {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    }:
        sent_at = 2.0
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=tradeoffer_id,
            offer_attempted_at=attempted_at,
            offer_sent_at=sent_at,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision,
    )


def test_permit_metadata_repr_and_errors_are_secret_free():
    permit = _permit()
    metadata = permit.metadata(phase="armed")
    forbidden_fields = {
        "cookie",
        "steamloginsecure",
        "sessionid",
        "identity_secret",
        "shared_secret",
        "access_token",
        "refresh_token",
        "authorization",
        "signature",
        "nonce",
        "buyer_info",
        "http_body",
        "http_headers",
    }
    field_names = {field.name.lower() for field in dataclasses.fields(CanaryPermit)}
    metadata_names = {str(key).lower() for key in metadata}
    assert not field_names & forbidden_fields
    assert not metadata_names & forbidden_fields
    encoded = json.dumps(metadata, sort_keys=True).lower()
    assert "identity_secret" not in encoded
    assert "steamloginsecure" not in encoded
    assert "sessionid" not in encoded

    sentinel = "SECRET_SENTINEL_DO_NOT_ECHO"
    with pytest.raises(CanaryAuthorityError) as error:
        CanaryWriteTarget(action="not-an-action", purchase_id=sentinel)
    assert sentinel not in str(error.value)
    assert sentinel not in repr(error.value)
    assert sentinel not in repr(permit)


def test_production_root_is_not_selected_by_home_environment(monkeypatch, tmp_path):
    import app.auto_offer.canary_authority as module

    before = module._production_root()
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-profile"))
    assert module._production_root() == before



def test_live_owner_excludes_second_authority_and_public_mint_apis_are_disabled(tmp_path):
    owner = CanaryAuthority(_root=tmp_path)
    contender = CanaryAuthority(_root=tmp_path)
    permit = _permit()
    session = owner._arm_owner_session(permit)

    with pytest.raises(CanaryAuthorityBusyError):
        contender._arm_owner_session(_permit(permit_id="permit-2", owner_nonce="owner-2"))
    with pytest.raises(CanaryAuthorityError, match="canary_host_activation_required"):
        contender.arm(_permit(permit_id="permit-2", owner_nonce="owner-2"))
    with pytest.raises(CanaryAuthorityError, match="canary_owner_session_required"):
        owner.owner_runtime_guard(permit)

    session.release_keep_fence()
    with pytest.raises(CanaryAuthorityStaleError):
        contender._arm_owner_session(_permit(permit_id="permit-2", owner_nonce="owner-2"))
    with pytest.raises(CanaryAuthorityError, match="clear_stale_disabled_use_atomic_recovery"):
        contender.clear_stale(expected_permit_id="permit-1")
    with pytest.raises(CanaryAuthorityStaleError):
        with contender.runtime_guard():
            pass


def test_atomic_recovery_rotates_generation_and_prevents_permit_replay(tmp_path):
    first = CanaryAuthority(_root=tmp_path)
    first_session = first._arm_owner_session(_permit())
    assert first._read_record()["generation"] == 1
    first_session.release_keep_fence()

    second_permit = _permit(permit_id="permit-2", owner_nonce="owner-2", created_at=124.0)
    recovery = CanaryAuthority(_root=tmp_path)
    recovery_session = recovery._recover_owner_session(
        expected_old_permit_id="permit-1",
        new_permit=second_permit,
    )
    assert recovery._read_record()["generation"] == 2
    recovery_session.release_keep_fence()

    replay = CanaryAuthority(_root=tmp_path)
    with pytest.raises(CanaryAuthorityStaleError, match="authority_permit_replay"):
        replay._recover_owner_session(
            expected_old_permit_id="permit-2",
            new_permit=_permit(permit_id="permit-1", owner_nonce="owner-replay", created_at=125.0),
        )
    with pytest.raises(CanaryAuthorityError, match="canary_host_recovery_required"):
        replay.recover_and_rearm(
            expected_old_permit_id="permit-2",
            new_permit=_permit(permit_id="permit-3", owner_nonce="owner-3", created_at=126.0),
        )
    with pytest.raises(CanaryAuthorityStaleError):
        with replay.runtime_guard():
            pass


def test_atomic_recovery_holds_lock_continuously_and_failed_replace_keeps_old_fence(monkeypatch, tmp_path):
    first = CanaryAuthority(_root=tmp_path)
    first_session = first._arm_owner_session(_permit())
    first_session.release_keep_fence()

    recovery = CanaryAuthority(_root=tmp_path)
    normal = CanaryAuthority(_root=tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_write = recovery._write_record

    def delayed_write(record):
        entered.set()
        assert release.wait(timeout=10)
        return original_write(record)

    monkeypatch.setattr(recovery, "_write_record", delayed_write)
    errors = []
    sessions = []

    def rotate():
        try:
            sessions.append(recovery._recover_owner_session(
                expected_old_permit_id="permit-1",
                new_permit=_permit(permit_id="permit-2", owner_nonce="owner-2", created_at=124.0),
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=rotate)
    thread.start()
    assert entered.wait(timeout=10)
    with pytest.raises(CanaryAuthorityBusyError):
        with normal.runtime_guard():
            pass
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert len(sessions) == 1
    sessions[0].release_keep_fence()

    failed = CanaryAuthority(_root=tmp_path)
    monkeypatch.setattr(
        failed,
        "_write_record",
        lambda _record: (_ for _ in ()).throw(CanaryAuthorityError("injected_write_failure")),
    )
    with pytest.raises(CanaryAuthorityError, match="injected_write_failure"):
        failed._recover_owner_session(
            expected_old_permit_id="permit-2",
            new_permit=_permit(permit_id="permit-3", owner_nonce="owner-3", created_at=125.0),
        )
    with pytest.raises(CanaryAuthorityStaleError):
        with CanaryAuthority(_root=tmp_path).runtime_guard():
            pass


def test_process_death_releases_os_lock_but_recovery_requires_fresh_generation(tmp_path):
    root = Path(tmp_path)
    ready = root / "ready"
    script = r'''
import sys, time
from pathlib import Path
from app.auto_offer.canary_authority import CanaryAuthority, CanaryPermit
root, ready = Path(sys.argv[1]), Path(sys.argv[2])
permit = CanaryPermit(
    permit_id="child-permit", owner_nonce="child-owner", host_db_id=7,
    buff_order_id="buff-order-7", purchase_id="buff:buff-order-7",
    account_id="account-1", recipient_steam_id="76561198000000007",
    expected_host_order_ids=("buff-order-7",), expected_store_present=False,
    expected_store_revision=None, expected_store_status=None,
    expected_store_tradeoffer_id=None, created_at=123.0,
)
a = CanaryAuthority(_root=root)
a._arm_owner_session(permit)
ready.write_text("ready", encoding="utf-8")
while True: time.sleep(1)
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(root), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        contender = CanaryAuthority(_root=root)
        with pytest.raises(CanaryAuthorityBusyError):
            contender._arm_owner_session(_permit(permit_id="permit-2", owner_nonce="owner-2"))
        process.kill()
        process.wait(timeout=10)
        with pytest.raises(CanaryAuthorityStaleError):
            contender._arm_owner_session(_permit(permit_id="permit-2", owner_nonce="owner-2"))
        session = contender._recover_owner_session(
            expected_old_permit_id="child-permit",
            new_permit=_permit(permit_id="permit-2", owner_nonce="owner-2", created_at=124.0),
        )
        assert contender._read_record()["generation"] == 2
        session.release_keep_fence()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_owner_session_is_opaque_and_public_surfaces_cannot_self_mint(tmp_path):
    import app.auto_offer.canary_authority as module

    db_path = _host_db(tmp_path / "host.db")
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    permit = _permit()
    session = authority._arm_owner_session(permit)

    assert authority.validates_owner_session(session, permit) is True
    assert not hasattr(authority, "owner_permit")
    assert not hasattr(authority, "owner_generation")
    assert "permit-1" not in repr(session)
    assert "owner-1" not in repr(session)
    assert "_CanaryOwnerSession" not in module.__all__
    with pytest.raises(TypeError, match="canary_owner_session_not_serializable"):
        pickle.dumps(session)

    body_calls = []
    errors = []

    def ordinary_thread():
        try:
            authority.has_active_fence()
            authority.has_canary_metadata()
            with authority.external_write_guard(_target("auto_offer_send")):
                body_calls.append("write")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=ordinary_thread)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert body_calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], CanaryWriteBlockedError)
    assert str(errors[0]) == "canary_owner_session_required"

    with pytest.raises(CanaryAuthorityError, match="canary_owner_session_required"):
        authority.owner_runtime_guard(permit)
    with pytest.raises(CanaryAuthorityError, match="canary_owner_session_required"):
        authority.mark_completed()
    with pytest.raises(CanaryAuthorityError, match="canary_owner_session_required"):
        authority.release_keep_fence()

    with session.runtime_guard():
        with pytest.raises(CanaryWriteBlockedError, match="canary_owner_session_required"):
            with authority.external_write_guard(_target("auto_offer_send")):
                body_calls.append("callback")
    assert body_calls == []
    session.release_keep_fence()


def test_exact_send_and_confirm_require_private_session_and_hold_db_write_barrier(tmp_path):
    db_path = _host_db(tmp_path / "host.db")
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    session = authority._arm_owner_session(_permit())

    for action in ("auto_offer_send", "auto_offer_confirm"):
        direct_writer = []
        with session.external_write_guard(_target(action)):
            def try_direct_write():
                connection = sqlite3.connect(db_path, timeout=0.0, isolation_level=None)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO purchase(id,buff_order_id,pending_receipt,assetid) VALUES(8,'other',1,NULL)"
                    )
                    connection.commit()
                    direct_writer.append("wrote")
                except sqlite3.OperationalError:
                    direct_writer.append("locked")
                finally:
                    connection.close()

            thread = threading.Thread(target=try_direct_write)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert direct_writer == ["locked"]
    session.release_keep_fence()


def test_second_live_host_row_blocks_session_send_before_body(tmp_path):
    db_path = _host_db(
        tmp_path / "host.db",
        rows=((7, ORDER_ID, 1, None), (8, "buff-order-8", 1, None)),
    )
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    session = authority._arm_owner_session(_permit())
    calls = []
    with pytest.raises(CanaryWriteBlockedError, match="canary_host_target_not_exclusive"):
        with session.external_write_guard(_target("auto_offer_send")):
            calls.append("adapter")
    assert calls == []
    session.release_keep_fence()


def test_reentrant_callback_cannot_piggyback_legitimate_owner_write(tmp_path):
    db_path = _host_db(tmp_path / "host.db")
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    session = authority._arm_owner_session(_permit())
    public_calls = []
    session_calls = []

    with session.external_write_guard(_target("auto_offer_send")):
        with pytest.raises(CanaryWriteBlockedError, match="canary_owner_session_required"):
            with authority.external_write_guard(_target("auto_offer_send")):
                public_calls.append("public")
        with pytest.raises(CanaryWriteBlockedError, match="owner_write_reentry_forbidden"):
            with session.external_write_guard(_target("auto_offer_send")):
                session_calls.append("nested")
    assert public_calls == []
    assert session_calls == []
    session.release_keep_fence()


def test_exact_receipt_allows_one_state_refinement_only_under_private_session(tmp_path):
    authority = CanaryAuthority(_root=tmp_path)
    session = authority._arm_owner_session(_permit())
    calls = []
    outer = _target("host_receipt", db_id=7, assetid="asset-7")
    inner = CanaryWriteTarget(
        action="host_receipt",
        buff_order_id=ORDER_ID,
        host_db_id=7,
        assetid="asset-7",
    )
    with session.external_write_guard(outer):
        with authority.external_write_guard(inner):
            calls.append("receipt")
        with pytest.raises(CanaryWriteBlockedError, match="nested_receipt_already_consumed"):
            with authority.external_write_guard(inner):
                calls.append("duplicate")
    assert calls == ["receipt"]
    with pytest.raises(CanaryWriteBlockedError, match="canary_owner_session_required"):
        with authority.external_write_guard(inner):
            pass
    session.release_keep_fence()


def test_completed_session_disables_writes_and_completion_requires_no_pending_host_rows(tmp_path):
    db_path = _host_db(tmp_path / "host.db")
    authority = CanaryAuthority(_root=tmp_path / "authority", _host_db_path=db_path)
    session = authority._arm_owner_session(_permit())
    with pytest.raises(CanaryAuthorityError, match="canary_completion_host_pending"):
        session.mark_completed()
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE purchase SET pending_receipt=0, assetid='asset-7' WHERE id=7")
    connection.commit()
    connection.close()
    session.mark_completed()
    with pytest.raises(CanaryWriteBlockedError, match="canary_not_armed"):
        with session.external_write_guard(_target("auto_offer_send")):
            pass
    with pytest.raises(CanaryAuthorityError, match="canary_owner_session_required"):
        authority.release_keep_fence()
    session.release_keep_fence()

def test_normal_runtime_nested_guards_remain_compatible(tmp_path):
    authority = CanaryAuthority(_root=tmp_path)
    with authority.runtime_guard():
        with authority.external_write_guard(CanaryWriteTarget(action="buff_purchase")):
            pass
    with authority.external_write_guard(CanaryWriteTarget(action="buff_purchase")):
        with authority.runtime_guard():
            pass


def test_coordinator_persists_send_attempt_before_fenced_adapter_call():
    from app.auto_offer.adapters import PlatformCapability
    from app.auto_offer.coordinator import DeliveryCoordinator, ReadOnlyCoordinatorBlockedError

    item = _stored(DeliveryStatus.AWAITING_OFFER)

    class Store:
        def __init__(self):
            self.current = item

        def get_by_purchase_id(self, _purchase_id):
            return self.current

        def advance(self, current, target):
            self.current = StoredDelivery(target, current.revision + 1)
            return self.current

    class Adapter:
        capabilities = frozenset({PlatformCapability.SEND_OFFER})

        def __init__(self):
            self.calls = []

        def execute(self, request):
            self.calls.append(request)
            raise AssertionError("adapter must not run")

    @contextmanager
    def blocked(_request):
        raise CanaryWriteBlockedError("blocked")
        yield

    store, adapter = Store(), Adapter()
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.SEND_OFFER: adapter},
        timeout_seconds=1.0,
        allow_writes=True,
        write_guard=blocked,
        clock=lambda: 10.0,
    )
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="canary_write_blocked"):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED


def test_coordinator_persists_confirmation_attempt_before_fenced_adapter_call():
    from app.auto_offer.adapters import PlatformCapability
    from app.auto_offer.coordinator import DeliveryCoordinator, ReadOnlyCoordinatorBlockedError

    item = _stored(
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        revision=5,
        tradeoffer_id="offer-7",
    )

    class Store:
        def __init__(self):
            self.current = item

        def get_by_purchase_id(self, _purchase_id):
            return self.current

        def advance(self, current, target):
            self.current = StoredDelivery(target, current.revision + 1)
            return self.current

    class Adapter:
        capabilities = frozenset({PlatformCapability.CONFIRM_OFFER})

        def __init__(self):
            self.calls = []

        def execute(self, request):
            self.calls.append(request)
            raise AssertionError("adapter must not run")

    @contextmanager
    def blocked(_request):
        raise CanaryWriteBlockedError("blocked")
        yield

    store, adapter = Store(), Adapter()
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.CONFIRM_OFFER: adapter},
        timeout_seconds=1.0,
        allow_writes=True,
        allow_confirmation_writes=True,
        write_guard=blocked,
    )
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="canary_write_blocked"):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
