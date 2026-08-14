from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.auto_offer.host_integration as host_integration
import app.pipeline as pipeline
from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.host_integration import HostAutoOfferIntegrationError
from app.auto_offer.store import StoredDelivery
from app.pipeline_context import PipelineContext
from app.pipeline_steps import TARGET_REACHED


ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000001"
COOKIE = "steamLoginSecure=fake"


def _stored(
    order_id: str,
    *,
    status: DeliveryStatus = DeliveryStatus.PENDING_DIRECTION,
    mode: DeliveryMode | None = None,
    revision: int = 1,
    account_id: str = ACCOUNT_ID,
    recipient: str = STEAM_ID,
    assetid: str | None = None,
) -> StoredDelivery:
    attempted_at = None
    sent_at = None
    received_at = None
    tradeoffer_id = None
    pending_receipt = True
    error = None

    buyer_bound = {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.OFFER_TERMINATED,
        DeliveryStatus.RECEIVED,
    }
    seller_bound = {
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.OFFER_TERMINATED,
        DeliveryStatus.RECEIVED,
    }
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        attempted_at = 10.0
    if status is DeliveryStatus.RESULT_UNKNOWN:
        attempted_at = 10.0
        error = "write_result_unknown"
    if status is DeliveryStatus.OFFER_TERMINATED:
        error = "offer_terminated"
    if mode is DeliveryMode.BUYER_SENDS_OFFER and status in buyer_bound:
        attempted_at = 10.0
        sent_at = 11.0
        tradeoffer_id = f"offer-{order_id}"
    if mode is DeliveryMode.SELLER_SENDS_OFFER and status in seller_bound:
        tradeoffer_id = f"offer-{order_id}"
    if status is DeliveryStatus.RECEIVED:
        pending_receipt = False
        received_at = 12.0
        assetid = assetid or f"asset-{order_id}"

    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id=account_id,
            recipient_steam_id=recipient,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=tradeoffer_id,
            offer_attempted_at=attempted_at,
            offer_sent_at=sent_at,
            received_at=received_at,
            delivery_error=error,
            pending_receipt=pending_receipt,
            assetid=assetid,
        ),
        revision=revision,
    )


def _host_row(order_id: str, *, db_id: int = 1) -> dict:
    return {
        "_db_id": db_id,
        "pending_receipt": True,
        "assetid": None,
        "buff_order_id": order_id,
    }


def _local_host_db(path: Path, rows) -> Path:
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
            "INSERT INTO purchase(id,buff_order_id,pending_receipt,assetid) "
            "VALUES(?,?,?,?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


class FakeBridge:
    def __init__(self, recoverable=(), terminal=()):
        self.account_id = ACCOUNT_ID
        self.recipient_steam_id = STEAM_ID
        self.current = {
            item.snapshot.buff_order_id: item
            for item in tuple(recoverable) + tuple(terminal)
        }
        self.registered = []
        self.steps = []
        self.recoveries = []
        self.closed = False

    def register_committed_purchase(self, record):
        self.registered.append(record)
        return None

    def list_recoverable(self):
        return tuple(
            item
            for item in self.current.values()
            if item.snapshot.delivery_status is not DeliveryStatus.RECEIVED
        )

    def get_by_purchase_id(self, purchase_id):
        for item in self.current.values():
            if item.snapshot.purchase_id == purchase_id:
                return item
        return None

    def step(self, delivery):
        self.steps.append((delivery.snapshot.buff_order_id, delivery.snapshot.delivery_status))
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def recover_result_unknown_readonly(self, delivery):
        self.recoveries.append(
            (delivery.snapshot.buff_order_id, delivery.snapshot.delivery_status)
        )
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def close(self):
        self.closed = True


class RecoveryBridge(FakeBridge):
    def __init__(self, initial: StoredDelivery, transitions):
        super().__init__((initial,))
        self.transitions = dict(transitions)

    def step(self, delivery):
        order_id = delivery.snapshot.buff_order_id
        status = delivery.snapshot.delivery_status
        self.steps.append((order_id, status))
        after = self.transitions.get(status, delivery)
        self.current[order_id] = after
        return SimpleNamespace(
            after=after,
            persisted=after != delivery,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )


class MultiRecoveryBridge(FakeBridge):
    def __init__(self, initial, transitions=()):
        super().__init__(initial)
        self.transitions = dict(transitions)

    def step(self, delivery):
        order_id = delivery.snapshot.buff_order_id
        status = delivery.snapshot.delivery_status
        self.steps.append((order_id, status))
        after = self.transitions.get((order_id, status), delivery)
        self.current[order_id] = after
        return SimpleNamespace(
            after=after,
            persisted=after != delivery,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

class NormalSendBridge(FakeBridge):
    def __init__(self, deliveries, db_path):
        super().__init__(deliveries)
        self.db_path = db_path
        self.send_events = []
        self.proof = object()

    def get_by_purchase_id(self, purchase_id):
        delivery = super().get_by_purchase_id(purchase_id)
        self.send_events.append(("get", purchase_id, delivery.revision))
        return delivery

    def read_send_authority(self, delivery):
        self.send_events.append(("read_send_authority", delivery.revision))
        competing = sqlite3.connect(self.db_path, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                competing.execute("BEGIN IMMEDIATE")
        finally:
            competing.close()
        return self.proof

    def send_offer_with_authority(self, delivery, proof):
        assert proof is self.proof
        self.proof = None
        attempted = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
                offer_attempted_at=10.0,
            ),
            delivery.revision + 1,
        )
        request = PlatformRequest(
            purchase_id=attempted.snapshot.purchase_id,
            buff_order_id=attempted.snapshot.buff_order_id,
            account_id=attempted.snapshot.account_id,
            recipient_steam_id=attempted.snapshot.recipient_steam_id,
            revision=attempted.revision,
            capability=PlatformCapability.SEND_OFFER,
            timeout_seconds=5.0,
        )
        platform_result = PlatformResult(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "offer_created_unproven",
        )
        after = StoredDelivery(
            replace(
                attempted.snapshot,
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
            ),
            attempted.revision + 1,
        )
        self.current[delivery.snapshot.buff_order_id] = after
        self.send_events.append(("send", attempted.revision))
        return SimpleNamespace(
            before=delivery,
            attempted=attempted,
            platform_result=platform_result,
            after=after,
        )


class ExactRecoveryBridge(FakeBridge):
    def __init__(self, deliveries, *, malformed=False):
        super().__init__(deliveries)
        self.malformed = malformed

    def recover_result_unknown_readonly(self, delivery):
        order_id = delivery.snapshot.buff_order_id
        self.recoveries.append((order_id, delivery.snapshot.delivery_status))
        if self.malformed:
            return SimpleNamespace(
                after=delivery,
                persisted=False,
                decision=SimpleNamespace(result=AutoOfferResult.BLOCKED),
            )
        after = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_SENT,
                steam_tradeoffer_id=f"offer-{order_id}",
                counterparty_steam_id="76561198000000002",
                offer_sent_at=12.0,
                delivery_error=None,
            ),
            delivery.revision + 1,
        )
        self.current[order_id] = after
        return SimpleNamespace(
            after=after,
            persisted=True,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )


def _patch_identity(monkeypatch, *, account_id=ACCOUNT_ID, steam_id=STEAM_ID):
    monkeypatch.setattr(host_integration, "get_current_id", lambda: account_id)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": steam_id},
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": steam_id, "cookies": COOKIE},
    )


def test_default_off_and_invalid_flag_are_fail_closed(monkeypatch):
    assert host_integration.is_auto_offer_enabled({}) is False
    assert host_integration.is_auto_offer_enabled({"auto_offer": {}}) is False

    with pytest.raises(HostAutoOfferIntegrationError):
        host_integration.is_auto_offer_enabled(
            {"auto_offer": {"enabled": "true"}}
        )

    def tripwire(*_args, **_kwargs):
        raise AssertionError("disabled path inspected Auto Offer dependencies")

    monkeypatch.setattr(host_integration, "get_current_id", tripwire)
    monkeypatch.setattr(host_integration, "get_account", tripwire)
    monkeypatch.setattr(host_integration, "get_steam_credentials", tripwire)
    monkeypatch.setattr(
        host_integration,
        "_build_active_host_auto_offer_bridge",
        tripwire,
    )
    assert (
        host_integration.build_host_auto_offer_integration(
            config={"auto_offer": {"enabled": False}},
            buff_client=object(),
        )
        is None
    )


def test_enabled_builder_uses_exact_current_account_existing_client_and_fixed_store_path(
    monkeypatch,
):
    _patch_identity(monkeypatch)
    calls = []
    bridge = FakeBridge()

    def build_bridge(**kwargs):
        calls.append(kwargs)
        return bridge

    monkeypatch.setattr(
        host_integration,
        "_build_active_host_auto_offer_bridge",
        build_bridge,
    )
    buyer = object()
    writer = lambda *_args: True
    integration = host_integration.build_host_auto_offer_integration(
        config={"auto_offer": {"enabled": True}},
        buff_client=buyer,
        complete_purchase_receipt_by_id=writer,
    )

    assert integration is not None
    assert calls[0]["buff_client"] is buyer
    assert calls[0]["account_id"] == ACCOUNT_ID
    assert calls[0]["account_steam_id"] == STEAM_ID
    assert calls[0]["store_path"] == (
        Path(host_integration.__file__).resolve().parents[2]
        / "config"
        / "auto_offer.db"
    )
    integration.close()
    assert bridge.closed is True


def test_enabled_builder_requires_receipt_writer_before_bridge_build(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration,
        "_build_active_host_auto_offer_bridge",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("bridge built")),
    )
    with pytest.raises(HostAutoOfferIntegrationError, match="receipt_writer_required"):
        host_integration.build_host_auto_offer_integration(
            config={"auto_offer": {"enabled": True}},
            buff_client=object(),
        )


def test_current_identity_does_not_use_legacy_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(host_integration, "get_current_id", lambda: ACCOUNT_ID)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda account_id: calls.append(account_id)
        or {"id": account_id, "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(
        host_integration,
        "get_current_account",
        lambda: (_ for _ in ()).throw(AssertionError("fallback used")),
        raising=False,
    )
    assert host_integration._exact_current_account() == (ACCOUNT_ID, STEAM_ID)
    assert calls == [ACCOUNT_ID]


@pytest.mark.parametrize(
    ("current_id", "account", "credential_steam_id"),
    [
        (None, {"id": ACCOUNT_ID, "steam_id": STEAM_ID}, STEAM_ID),
        ("", {"id": ACCOUNT_ID, "steam_id": STEAM_ID}, STEAM_ID),
        (" ", {"id": ACCOUNT_ID, "steam_id": STEAM_ID}, STEAM_ID),
        (ACCOUNT_ID, None, STEAM_ID),
        (ACCOUNT_ID, {"id": "different-account", "steam_id": STEAM_ID}, STEAM_ID),
        (ACCOUNT_ID, {"id": ACCOUNT_ID, "steam_id": None}, STEAM_ID),
        (ACCOUNT_ID, {"id": ACCOUNT_ID, "steam_id": ""}, ""),
        (ACCOUNT_ID, {"id": ACCOUNT_ID, "steam_id": " "}, " "),
        (ACCOUNT_ID, {"id": ACCOUNT_ID, "steam_id": "not-canonical"}, "not-canonical"),
        (ACCOUNT_ID, {"id": ACCOUNT_ID, "steam_id": "01"}, "01"),
    ],
)
def test_invalid_current_identity_fails_closed_without_first_account_fallback(
    monkeypatch, current_id, account, credential_steam_id
):
    monkeypatch.setattr(host_integration, "get_current_id", lambda: current_id)
    monkeypatch.setattr(host_integration, "get_account", lambda _account_id: account)
    monkeypatch.setattr(
        host_integration,
        "get_current_account",
        lambda: (_ for _ in ()).throw(AssertionError("legacy first-account fallback used")),
        raising=False,
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": credential_steam_id, "cookies": COOKIE},
    )

    with pytest.raises(HostAutoOfferIntegrationError):
        host_integration.build_host_auto_offer_integration(
            config={"auto_offer": {"enabled": True}},
            buff_client=object(),
            complete_purchase_receipt_by_id=lambda *_args: True,
        )


@pytest.mark.parametrize(
    ("host_rows", "stored", "expected"),
    [
        ([], [], AutoOfferResult.COMPLETE),
        (
            [_host_row("order-1")],
            [_stored("order-1")],
            AutoOfferResult.WAITING,
        ),
        (
            [_host_row("order-1")],
            [],
            AutoOfferResult.BLOCKED,
        ),
        (
            [_host_row("order-1")],
            [_stored("order-2")],
            AutoOfferResult.BLOCKED,
        ),
        (
            [{"_db_id": 1, "pending_receipt": True, "assetid": None, "buff_order_id": ""}],
            [],
            AutoOfferResult.BLOCKED,
        ),
    ],
)
def test_next_purchase_gate_uses_exact_host_and_store_sets(
    monkeypatch, host_rows, stored, expected
):
    _patch_identity(monkeypatch)
    bridge = FakeBridge(stored)
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )
    assert integration.next_purchase_result(host_rows) is expected
    assert bridge.steps == []
    assert bridge.recoveries == []
    assert writes == []


def test_next_purchase_gate_rejects_identity_mismatch(monkeypatch):
    _patch_identity(monkeypatch)
    bridge = FakeBridge([_stored("order-1", recipient="76561198000000002")])
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )
    assert integration.next_purchase_result(
        [_host_row("order-1")]
    ) is AutoOfferResult.BLOCKED

    _patch_identity(monkeypatch, account_id="account-2")
    assert integration.next_purchase_result([]) is AutoOfferResult.BLOCKED
    assert bridge.steps == []
    assert bridge.recoveries == []
    assert writes == []


def test_pending_host_with_existing_asset_is_inconsistent_and_blocks(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored("order-1")
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    row = _host_row("order-1")
    row["assetid"] = "asset-already-present"

    assert integration.next_purchase_result([row]) is AutoOfferResult.BLOCKED
    assert bridge.steps == []


def test_normal_admission_is_verdict_only_for_exact_safe_pending_sets(monkeypatch):
    _patch_identity(monkeypatch)
    bridge = FakeBridge((_stored("order-1"),))
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == []
    assert writes == []


def test_normal_admission_extra_store_result_unknown_blocks_without_side_effects(
    monkeypatch,
):
    _patch_identity(monkeypatch)
    unknown = _stored(
        "order-2",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = FakeBridge((_stored("order-1"), unknown))
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.BLOCKED
    assert bridge.steps == []
    assert bridge.recoveries == []
    assert writes == []


def test_normal_admission_exact_result_unknown_is_global_and_does_not_step(
    monkeypatch,
):
    _patch_identity(monkeypatch)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = FakeBridge((unknown,))
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.RESULT_UNKNOWN
    assert bridge.steps == []
    assert bridge.recoveries == []
    assert writes == []


def test_normal_admission_received_is_waiting_without_receipt_write(monkeypatch):
    _patch_identity(monkeypatch)
    received = _stored(
        "order-1",
        status=DeliveryStatus.RECEIVED,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        revision=8,
        assetid="asset-exact",
    )
    bridge = FakeBridge(terminal=(received,))
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == []
    assert writes == []


def test_normal_registration_persists_without_enqueuing_dispatch_authority(monkeypatch):
    _patch_identity(monkeypatch)
    fresh = _stored("order-1")

    class RegisteringBridge(FakeBridge):
        def register_committed_purchase(self, record):
            self.registered.append(record)
            self.current["order-1"] = fresh
            return fresh

    bridge = RegisteringBridge()
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_host_row("order-1"))

    assert [item["buff_order_id"] for item in bridge.registered] == ["order-1"]
    assert integration._fresh_deliveries == []
    assert bridge.steps == []


def test_normal_close_only_closes_owned_resources(monkeypatch):
    _patch_identity(monkeypatch)
    bridge = FakeBridge((_stored("order-1"),))
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *_args: True,
    )

    integration.close()

    assert bridge.closed is True
    assert bridge.steps == []


def test_delivery_tick_pending_direction_performs_one_read_only(monkeypatch):
    _patch_identity(monkeypatch)
    initial = _stored("order-1")
    awaiting = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    bridge = RecoveryBridge(initial, {DeliveryStatus.PENDING_DIRECTION: awaiting})
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([_host_row("order-1")])

    assert outcome == host_integration.DeliveryTickOutcome(
        AutoOfferResult.WAITING,
        "order-1",
        ("order-1",),
    )
    assert bridge.steps == [("order-1", DeliveryStatus.PENDING_DIRECTION)]


def test_adjacent_readable_transitions_require_separate_ticks_and_receipt_tick(monkeypatch):
    _patch_identity(monkeypatch)
    confirmed = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    awaiting = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_INVENTORY,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=6,
    )
    received = _stored(
        "order-1",
        status=DeliveryStatus.RECEIVED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=7,
        assetid="asset-exact",
    )
    bridge = RecoveryBridge(
        confirmed,
        {
            DeliveryStatus.OFFER_CONFIRMED: awaiting,
            DeliveryStatus.AWAITING_INVENTORY: received,
        },
    )
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )
    rows = [_host_row("order-1", db_id=41)]

    integration.run_delivery_tick(rows)
    assert bridge.steps == [("order-1", DeliveryStatus.OFFER_CONFIRMED)]
    assert writes == []

    integration.run_delivery_tick(rows, cursor="order-1")
    assert bridge.steps == [
        ("order-1", DeliveryStatus.OFFER_CONFIRMED),
        ("order-1", DeliveryStatus.AWAITING_INVENTORY),
    ]
    assert writes == []

    integration.run_delivery_tick(rows, cursor="order-1")
    assert len(bridge.steps) == 2
    assert writes == [(41, "order-1", "asset-exact")]


def test_normal_buyer_confirmation_required_remains_safe_wait(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([_host_row("order-1")])

    assert outcome.result is AutoOfferResult.WAITING
    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.steps == []


def test_normal_send_is_one_host_barrier_action_and_stops_at_result_unknown(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    db_path = _local_host_db(
        tmp_path / "host.db",
        ((41, "order-1", 1, None), (42, "order-2", 1, None)),
    )
    authority = host_integration.CanaryAuthority(
        _root=tmp_path / "authority",
        _host_db_path=db_path,
    )
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    awaiting = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    bridge = NormalSendBridge((awaiting, _stored("order-2")), db_path)
    receipt_writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=(
            lambda *args: receipt_writes.append(args) or True
        ),
    )

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=41), _host_row("order-2", db_id=42)]
    )

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.send_events == [
        ("get", "buff:order-1", 5),
        ("read_send_authority", 5),
        ("send", 6),
    ]
    assert bridge.steps == []
    assert bridge.proof is None
    after = bridge.current["order-1"].snapshot
    assert after.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert after.delivery_error == "write_result_unknown"
    assert after.steam_tradeoffer_id is None
    assert after.counterparty_steam_id is None
    assert receipt_writes == []


def test_exact_result_unknown_recovery_ends_tick_before_normal_progression(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = ExactRecoveryBridge((unknown, _stored("order-2")))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.result is AutoOfferResult.WAITING
    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.recoveries == [("order-1", DeliveryStatus.RESULT_UNKNOWN)]
    assert bridge.steps == []
    recovered = bridge.current["order-1"].snapshot
    assert recovered.delivery_status is DeliveryStatus.OFFER_SENT
    assert recovered.steam_tradeoffer_id == "offer-order-1"
    assert recovered.counterparty_steam_id == "76561198000000002"


def test_missing_host_row_blocks_before_result_unknown_recovery(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = ExactRecoveryBridge((unknown,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([])

    assert outcome.result is AutoOfferResult.BLOCKED
    assert outcome.visited_order_ids == ()
    assert bridge.recoveries == []
    assert bridge.steps == []
    assert bridge.current["order-1"] == unknown


def test_extra_host_pending_order_blocks_before_result_unknown_recovery(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = ExactRecoveryBridge((unknown,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.result is AutoOfferResult.BLOCKED
    assert outcome.visited_order_ids == ()
    assert bridge.recoveries == []
    assert bridge.steps == []
    assert bridge.current["order-1"] == unknown


def test_extra_store_order_blocks_before_result_unknown_recovery(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    extra = _stored("order-2")
    bridge = ExactRecoveryBridge((unknown, extra))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([_host_row("order-1")])

    assert outcome.result is AutoOfferResult.BLOCKED
    assert outcome.visited_order_ids == ()
    assert bridge.recoveries == []
    assert bridge.steps == []
    assert bridge.current["order-1"] == unknown
    assert bridge.current["order-2"] == extra


def test_unresolved_checkout_defers_result_unknown_recovery_without_progression(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: False),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = ExactRecoveryBridge((unknown,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([_host_row("order-1")], cursor="order-0")

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert outcome.next_cursor == "order-0"
    assert outcome.visited_order_ids == ()
    assert bridge.recoveries == []
    assert bridge.steps == []
    assert bridge.current["order-1"] == unknown


def test_malformed_result_unknown_recovery_blocks_without_other_progression(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = ExactRecoveryBridge((unknown, _stored("order-2")), malformed=True)
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.result is AutoOfferResult.BLOCKED
    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.recoveries == [("order-1", DeliveryStatus.RESULT_UNKNOWN)]
    assert bridge.steps == []
    assert bridge.current["order-1"] == unknown


def test_recovery_only_tick_is_bounded_and_cursor_fair(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    orders = tuple(f"order-{index:02d}" for index in range(10))
    unknowns = tuple(
        _stored(
            order,
            status=DeliveryStatus.RESULT_UNKNOWN,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=3,
        )
        for order in orders
    )
    bridge = FakeBridge(unknowns)
    integration = host_integration.HostAutoOfferIntegration(bridge)
    rows = [_host_row(order, db_id=index + 1) for index, order in enumerate(orders)]

    first = integration.run_delivery_tick(rows)
    second = integration.run_delivery_tick(rows, cursor=first.next_cursor)

    assert first.result is second.result is AutoOfferResult.RESULT_UNKNOWN
    assert first.visited_order_ids == orders[:8]
    assert first.next_cursor == "order-07"
    assert second.visited_order_ids == (
        "order-08",
        "order-09",
        "order-00",
        "order-01",
        "order-02",
        "order-03",
        "order-04",
        "order-05",
    )
    assert bridge.steps == []
    assert len(bridge.recoveries) == 16


def test_bound_confirmation_result_unknown_is_not_c2a_auto_recovered(
    monkeypatch,
    tmp_path,
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    unknown = _stored(
        "order-1",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=6,
    )
    bound = StoredDelivery(
        replace(
            unknown.snapshot,
            steam_tradeoffer_id="offer-1",
            counterparty_steam_id="76561198000000002",
            offer_sent_at=10.0,
        ),
        unknown.revision,
    )
    bridge = FakeBridge((bound,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick([_host_row("order-1")])

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert outcome.visited_order_ids == ()
    assert bridge.recoveries == []
    assert bridge.steps == []


def test_offer_terminated_quarantine_does_not_starve_later_safe_order(monkeypatch):
    _patch_identity(monkeypatch)
    terminated = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_TERMINATED,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        revision=4,
    )
    readable = _stored("order-2")
    bridge = MultiRecoveryBridge((terminated, readable))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.visited_order_ids == ("order-1", "order-2")
    assert bridge.steps == [("order-2", DeliveryStatus.PENDING_DIRECTION)]


def test_delivery_tick_result_unknown_short_circuits_every_order(monkeypatch):
    _patch_identity(monkeypatch)
    unknown = _stored(
        "order-2",
        status=DeliveryStatus.RESULT_UNKNOWN,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=3,
    )
    bridge = MultiRecoveryBridge((_stored("order-1"), unknown))
    writes = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert outcome.visited_order_ids == ("order-2",)
    assert bridge.steps == []
    assert bridge.recoveries == [("order-2", DeliveryStatus.RESULT_UNKNOWN)]
    assert writes == []


def test_delivery_tick_order_and_cursor_are_deterministic(monkeypatch):
    _patch_identity(monkeypatch)
    orders = ("order-c", "order-a", "order-b")
    bridge = MultiRecoveryBridge(tuple(_stored(order) for order in orders))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    rows = [_host_row(order, db_id=index) for index, order in enumerate(orders, 1)]

    first = integration.run_delivery_tick(rows)
    resumed = integration.run_delivery_tick(rows, cursor="order-a")
    stale = integration.run_delivery_tick(rows, cursor="order-bb")
    wrapped = integration.run_delivery_tick(rows, cursor="order-z")

    assert first.visited_order_ids == ("order-a", "order-b", "order-c")
    assert resumed.visited_order_ids == ("order-b", "order-c", "order-a")
    assert stale.visited_order_ids == ("order-c", "order-a", "order-b")
    assert wrapped.visited_order_ids == ("order-a", "order-b", "order-c")


def test_safe_wait_advances_cursor_and_allows_following_order(monkeypatch):
    _patch_identity(monkeypatch)
    waiting = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    bridge = MultiRecoveryBridge((waiting, _stored("order-2")))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    outcome = integration.run_delivery_tick(
        [_host_row("order-1", db_id=1), _host_row("order-2", db_id=2)]
    )

    assert outcome.next_cursor == "order-2"
    assert outcome.visited_order_ids == ("order-1", "order-2")
    assert bridge.steps == [("order-2", DeliveryStatus.PENDING_DIRECTION)]


def test_delivery_tick_visits_at_most_eight_and_steps_each_at_most_once(monkeypatch):
    _patch_identity(monkeypatch)
    orders = tuple(f"order-{index:02d}" for index in range(10))
    bridge = MultiRecoveryBridge(tuple(_stored(order) for order in reversed(orders)))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    rows = [_host_row(order, db_id=index + 1) for index, order in enumerate(orders)]

    outcome = integration.run_delivery_tick(rows)

    assert outcome.visited_order_ids == orders[:8]
    assert outcome.next_cursor == "order-07"
    assert bridge.steps == [
        (order, DeliveryStatus.PENDING_DIRECTION) for order in orders[:8]
    ]


def test_missing_exact_host_db_id_blocks_before_recovery_step(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored("order-1")
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result(
        [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}]
    ) is AutoOfferResult.BLOCKED
    assert bridge.steps == []


class PipelineFakeState:
    def __init__(self):
        self.purchases = []
        self.events = []
        self.statuses = []

    def get_purchases(self):
        return list(self.purchases)

    def append_purchase(self, purchase):
        self.events.append(("host_commit", purchase["buff_order_id"]))
        self.purchases.append(dict(purchase))

    def complete_purchase_receipt_by_id(self, *_args):
        self.events.append(("receipt_write",) + tuple(_args))
        return True

    def set_pending_payment(self, *_args, **_kwargs):
        pass

    def wait_payment_confirm(self, *_args, **_kwargs):
        return True

    def confirm_payment(self, *_args, **_kwargs):
        pass

    def is_stop_requested(self):
        return False

    def set_status(self, stage, message, **_kwargs):
        self.statuses.append((stage, message))

    def log(self, *_args, **_kwargs):
        pass


class FakePipelineIntegration:
    def __init__(self, results=(AutoOfferResult.COMPLETE,)):
        self.results = list(results)
        self.registered = []
        self.closed = False
        self.events = []

    def next_purchase_result(self, _purchases):
        if self.results:
            return self.results.pop(0)
        return AutoOfferResult.WAITING

    def register_committed_purchase(self, purchase):
        self.events.append(("registration", purchase["buff_order_id"]))
        self.registered.append(dict(purchase))

    def close(self):
        self.closed = True


def _run_pipeline_slice(monkeypatch, integration, state, *, target=1.0, checkout=None, config=None):
    if checkout is None:
        def checkout(*args, **kwargs):
            args[9]({
                "buff_order_id": "order-1",
                "pending_receipt": True,
                "assetid": None,
            })
            return 1.0

    monkeypatch.setattr(pipeline, "build_host_auto_offer_integration", lambda **_kwargs: integration)
    monkeypatch.setattr(
        pipeline,
        "pick_stable_item",
        lambda *_args, **_kwargs: (
            {"name": "fake", "goods_id": 1, "min_price": "1"},
            set(),
        ),
    )
    monkeypatch.setattr(pipeline, "lock_and_confirm_payment", checkout)
    monkeypatch.setattr(pipeline, "jittered_sleep", lambda *_args, **_kwargs: None)
    ctx = PipelineContext(state, "task022", verbose=False)
    cfg = config or {
        "auto_offer": {"enabled": True},
        "buff": {"auto_ask_seller_to_send": True},
    }
    return pipeline._process_deals_for_target(
        ctx,
        [{"name": "fake", "goods_id": 1}],
        cfg,
        target,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
    )


def test_pipeline_injects_host_owned_exact_receipt_writer(monkeypatch):
    state = PipelineFakeState()
    integration = FakePipelineIntegration()
    captured = []

    def build(**kwargs):
        captured.append(kwargs)
        return integration

    monkeypatch.setattr(pipeline, "build_host_auto_offer_integration", build)
    monkeypatch.setattr(
        pipeline,
        "pick_stable_item",
        lambda *_args, **_kwargs: (None, set()),
    )
    ctx = PipelineContext(state, "task032", verbose=False)
    pipeline._process_deals_for_target(
        ctx,
        [],
        {"auto_offer": {"enabled": True}, "buff": {}},
        1.0,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
    )

    assert len(captured) == 1
    writer = captured[0]["complete_purchase_receipt_by_id"]
    assert callable(writer)
    assert writer(7, "order-7", "asset-7") is True
    assert state.events == [("receipt_write", 7, "order-7", "asset-7")]


def test_host_commit_precedes_registration_and_seller_reminder_is_ephemeral(monkeypatch):
    state = PipelineFakeState()
    integration = FakePipelineIntegration()
    seen_configs = []

    def checkout(*args, **kwargs):
        seen_configs.append(args[2])
        args[9]({
            "buff_order_id": "order-1",
            "pending_receipt": True,
            "assetid": None,
        })
        return 1.0

    result = _run_pipeline_slice(
        monkeypatch,
        integration,
        state,
        checkout=checkout,
        config={"auto_offer": {"enabled": True}, "buff": {"auto_ask_seller_to_send": True}},
    )

    assert result == (1.0, 1, False)
    assert state.events == [("host_commit", "order-1")]
    assert integration.events == [("registration", "order-1")]
    assert seen_configs[0]["buff"]["auto_ask_seller_to_send"] is False
    assert integration.closed is True


def test_gate_runs_before_second_purchase(monkeypatch):
    state = PipelineFakeState()
    integration = FakePipelineIntegration(
        results=(AutoOfferResult.COMPLETE, AutoOfferResult.WAITING)
    )
    result = _run_pipeline_slice(monkeypatch, integration, state, target=2.0)

    assert result == (2.0, 2, False)
    assert len(integration.registered) == 2
    assert integration.closed is True
    assert not any(message == "AUTO_OFFER_WAITING" for _, message in state.statuses)


def test_registration_failure_is_fail_closed_after_host_commit(monkeypatch):
    state = PipelineFakeState()

    class FailingIntegration(FakePipelineIntegration):
        def register_committed_purchase(self, purchase):
            self.events.append(("registration", purchase["buff_order_id"]))
            raise HostAutoOfferIntegrationError("registration failed")

    integration = FailingIntegration()
    with pytest.raises(HostAutoOfferIntegrationError):
        _run_pipeline_slice(monkeypatch, integration, state)

    assert state.events == [("host_commit", "order-1")]
    assert integration.events == [("registration", "order-1")]
    assert integration.closed is True


def test_bridge_closes_when_post_build_config_preparation_fails(monkeypatch):
    state = PipelineFakeState()
    integration = FakePipelineIntegration()

    def fail_deepcopy(_config):
        raise RuntimeError("ephemeral config preparation failed")

    monkeypatch.setattr(pipeline.copy, "deepcopy", fail_deepcopy)
    with pytest.raises(RuntimeError, match="ephemeral config preparation failed"):
        _run_pipeline_slice(monkeypatch, integration, state)

    assert integration.closed is True


def test_batch_records_register_exact_order_ids(monkeypatch):
    state = PipelineFakeState()
    integration = FakePipelineIntegration()

    def batch_checkout(*_args, **kwargs):
        for order_id in ("batch-order-1", "batch-order-2"):
            _args[9]({
                "buff_order_id": order_id,
                "pending_receipt": True,
                "assetid": None,
            })
        return TARGET_REACHED

    _run_pipeline_slice(
        monkeypatch,
        integration,
        state,
        target=2.0,
        checkout=batch_checkout,
    )
    assert [record["buff_order_id"] for record in integration.registered] == [
        "batch-order-1",
        "batch-order-2",
    ]
    assert state.events == [
        ("host_commit", "batch-order-1"),
        ("host_commit", "batch-order-2"),
    ]


def test_enabled_receive_worker_skips_legacy_transaction(monkeypatch):
    import app.receive_flow as receive_flow
    import app.services.buff_auth as buff_auth
    import app.services.buff_checkout_guard as buff_checkout_guard
    import app.services.buff_client as buff_client_module
    import app.services.workers as workers
    from contextlib import nullcontext

    class StopWorker(BaseException):
        pass

    sleep_calls = []
    background_gate_calls = []
    tick_calls = []

    def controlled_sleep(_seconds):
        sleep_calls.append(_seconds)
        if len(sleep_calls) == 2:
            raise StopWorker()

    monkeypatch.setattr(
        workers,
        "load_app_config_validated",
        lambda: {"auto_offer": {"enabled": True}, "pipeline": {}},
    )
    monkeypatch.setattr(workers.time, "sleep", controlled_sleep)
    monkeypatch.setattr(workers, "is_auto_offer_enabled", lambda _cfg: True)
    monkeypatch.setattr(
        workers,
        "is_steam_background_allowed",
        lambda: background_gate_calls.append(True) or True,
    )
    monkeypatch.setattr(
        workers,
        "get_buff_credentials",
        lambda: {"cookies": {"session": "fake"}},
    )
    monkeypatch.setattr(workers, "_buff_background_request_is_safe", lambda: True)
    monkeypatch.setattr(
        workers,
        "get_purchases",
        lambda: [{"pending_receipt": True, "assetid": None}],
    )
    monkeypatch.setattr(
        workers,
        "_run_auto_offer_delivery_tick",
        lambda *_args, **kwargs: tick_calls.append(kwargs.get("cursor"))
        or host_integration.DeliveryTickOutcome(
            AutoOfferResult.WAITING,
            "order-1",
            ("order-1",),
        ),
    )
    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", nullcontext)
    monkeypatch.setattr(buff_checkout_guard, "buff_activity_guard", nullcontext)
    monkeypatch.setattr(
        buff_client_module,
        "create_buff_client_from_config",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        receive_flow,
        "try_receive_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy receive transaction used")
        ),
    )

    with pytest.raises(StopWorker):
        workers.receive_worker()
    assert sleep_calls == [30, 30]
    assert background_gate_calls == [True, True]
    assert tick_calls == [None]


def test_disabled_receive_worker_keeps_legacy_path_without_auto_offer_runtime(monkeypatch):
    import app.receive_flow as receive_flow
    import app.services.buff_auth as buff_auth
    import app.services.buff_checkout_guard as buff_checkout_guard
    import app.services.buff_client as buff_client_module
    import app.services.workers as workers
    from contextlib import nullcontext

    class StopWorker(BaseException):
        pass

    sleep_calls = []
    legacy_calls = []

    def controlled_sleep(_seconds):
        sleep_calls.append(_seconds)
        if len(sleep_calls) == 2:
            raise StopWorker()

    monkeypatch.setattr(
        workers,
        "load_app_config_validated",
        lambda: {"auto_offer": {"enabled": False}, "pipeline": {}},
    )
    monkeypatch.setattr(workers.time, "sleep", controlled_sleep)
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(workers, "_buff_background_request_is_safe", lambda: True)
    monkeypatch.setattr(
        workers,
        "get_purchases",
        lambda: [{"pending_receipt": True, "assetid": None}],
    )
    monkeypatch.setattr(
        workers,
        "get_buff_credentials",
        lambda: {"cookies": {"session": "fake"}},
    )
    monkeypatch.setattr(
        workers,
        "build_host_auto_offer_integration",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Auto Offer runtime built")),
    )
    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", nullcontext)
    monkeypatch.setattr(buff_checkout_guard, "buff_activity_guard", nullcontext)
    monkeypatch.setattr(
        buff_client_module,
        "create_buff_client_from_config",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        receive_flow,
        "try_receive_once",
        lambda *_args, **_kwargs: legacy_calls.append(True) or 0,
    )

    with pytest.raises(StopWorker):
        workers.receive_worker()

    assert legacy_calls == [True]


def test_task034_runtime_markers_remain_confined_to_existing_host_seam():
    untouched_host_files = [
        Path("app/config_schema.py"),
        Path("app/services/workers.py"),
    ]
    forbidden = (
        "buyer_send_offer",
        "SEND_OFFER",
        "CONFIRM_OFFER",
        "ACCEPT_OFFER",
        ".step(",
        "accept_steam_trade_offer",
    )
    for path in untouched_host_files:
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{marker} found in {path}"

    host_source = Path("app/auto_offer/host_integration.py").read_text(encoding="utf-8")
    assert "SteamTradeOfferConfirmationAdapter" in host_source
    assert "SteamTradeOfferConfirmationTransport" in host_source
    for marker in (
        "DeliveryExecutor",
        "threading.Thread",
        "time.sleep(",
        "accept_all",
        "multiajaxop",
        "app.steam_confirm",
        "_make_request(",
        ".execute(",
    ):
        assert marker not in host_source, f"{marker} found in host integration"
