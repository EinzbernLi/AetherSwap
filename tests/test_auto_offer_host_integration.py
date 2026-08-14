from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.auto_offer.host_integration as host_integration
import app.pipeline as pipeline
from app.auto_offer.adapters import PlatformResultStatus
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
        DeliveryStatus.RECEIVED,
    }
    seller_bound = {
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    }
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        attempted_at = 10.0
    if status is DeliveryStatus.RESULT_UNKNOWN:
        attempted_at = 10.0
        error = "write_result_unknown"
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


class ConfirmationBridge(FakeBridge):
    def __init__(self, initial: StoredDelivery, status: PlatformResultStatus):
        super().__init__((initial,))
        self.confirmation_status = status

    def step(self, delivery):
        order_id = delivery.snapshot.buff_order_id
        status = delivery.snapshot.delivery_status
        self.steps.append((order_id, status))
        if status is not DeliveryStatus.OFFER_CONFIRMATION_REQUIRED:
            return SimpleNamespace(
                after=delivery,
                persisted=False,
                decision=SimpleNamespace(result=AutoOfferResult.WAITING),
            )

        attempted = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            ),
            delivery.revision + 1,
        )
        if self.confirmation_status is PlatformResultStatus.SUCCESS:
            after = StoredDelivery(
                replace(
                    attempted.snapshot,
                    delivery_status=DeliveryStatus.OFFER_CONFIRMED,
                ),
                attempted.revision + 1,
            )
        elif self.confirmation_status is PlatformResultStatus.RESULT_UNKNOWN:
            after = StoredDelivery(
                replace(
                    attempted.snapshot,
                    delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                    delivery_error="write_result_unknown",
                ),
                attempted.revision + 1,
            )
        else:
            after = attempted
        self.current[order_id] = after
        return SimpleNamespace(
            attempted=attempted,
            platform_result=SimpleNamespace(status=self.confirmation_status),
            after=after,
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
    integration = host_integration.HostAutoOfferIntegration(FakeBridge(stored))
    assert integration.next_purchase_result(host_rows) is expected


def test_next_purchase_gate_rejects_identity_mismatch(monkeypatch):
    _patch_identity(monkeypatch)
    integration = host_integration.HostAutoOfferIntegration(
        FakeBridge([_stored("order-1", recipient="76561198000000002")])
    )
    assert integration.next_purchase_result(
        [_host_row("order-1")]
    ) is AutoOfferResult.BLOCKED

    _patch_identity(monkeypatch, account_id="account-2")
    assert integration.next_purchase_result([]) is AutoOfferResult.BLOCKED


def test_pending_host_with_existing_asset_is_inconsistent_and_blocks(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored("order-1")
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    row = _host_row("order-1")
    row["assetid"] = "asset-already-present"

    assert integration.next_purchase_result([row]) is AutoOfferResult.BLOCKED
    assert bridge.steps == []


def test_persisted_buyer_awaiting_offer_never_steps_or_sends(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
    )
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == []


def test_persisted_pending_direction_may_read_then_stops_before_buyer_send(monkeypatch):
    _patch_identity(monkeypatch)
    initial = _stored("order-1")
    awaiting = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    bridge = RecoveryBridge(
        initial,
        {DeliveryStatus.PENDING_DIRECTION: awaiting},
    )
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == [("order-1", DeliveryStatus.PENDING_DIRECTION)]


def test_persisted_no_progress_read_stops_after_one_step(monkeypatch):
    _patch_identity(monkeypatch)
    initial = _stored(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
    )
    bridge = RecoveryBridge(initial, {})
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == [("order-1", DeliveryStatus.AWAITING_OFFER)]


def test_persisted_confirmation_success_stops_current_gate_at_confirmed(monkeypatch):
    _patch_identity(monkeypatch)
    required = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    bridge = ConfirmationBridge(required, PlatformResultStatus.SUCCESS)
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == [("order-1", DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)]
    assert bridge.current["order-1"].snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED


def test_persisted_confirmation_unknown_never_reconfirms_and_later_uses_read_path(monkeypatch):
    _patch_identity(monkeypatch)
    required = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    bridge = ConfirmationBridge(required, PlatformResultStatus.RESULT_UNKNOWN)
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.RESULT_UNKNOWN
    assert bridge.steps == [("order-1", DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)]
    assert bridge.current["order-1"].snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == [
        ("order-1", DeliveryStatus.OFFER_CONFIRMATION_REQUIRED),
        ("order-1", DeliveryStatus.RESULT_UNKNOWN),
    ]


def test_persisted_confirmation_known_failure_is_not_retried(monkeypatch):
    _patch_identity(monkeypatch)
    required = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=5,
    )
    bridge = ConfirmationBridge(required, PlatformResultStatus.FAILURE)
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.BLOCKED
    assert bridge.steps == [("order-1", DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)]
    assert bridge.current["order-1"].snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED

    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.WAITING
    assert bridge.steps == [
        ("order-1", DeliveryStatus.OFFER_CONFIRMATION_REQUIRED),
        ("order-1", DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED),
    ]


def test_bounded_adjacent_reads_reach_received_then_write_exact_host_receipt(monkeypatch):
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

    assert integration.next_purchase_result([_host_row("order-1", db_id=41)]) is AutoOfferResult.COMPLETE
    assert bridge.steps == [
        ("order-1", DeliveryStatus.OFFER_CONFIRMED),
        ("order-1", DeliveryStatus.AWAITING_INVENTORY),
    ]
    assert writes == [(41, "order-1", "asset-exact")]


def test_terminal_received_local_writeback_can_retry_without_platform_step(monkeypatch):
    _patch_identity(monkeypatch)
    received = _stored(
        "order-1",
        status=DeliveryStatus.RECEIVED,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        revision=8,
        assetid="asset-exact",
    )
    bridge = FakeBridge(terminal=(received,))
    failed = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *_args: False,
    )
    host_rows = [_host_row("order-1", db_id=51)]

    assert failed.next_purchase_result(host_rows) is AutoOfferResult.BLOCKED
    assert bridge.steps == []

    writes = []
    retried = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *args: writes.append(args) or True,
    )
    assert retried.next_purchase_result(host_rows) is AutoOfferResult.COMPLETE
    assert bridge.steps == []
    assert writes == [(51, "order-1", "asset-exact")]


def test_missing_exact_host_db_id_blocks_before_recovery_step(monkeypatch):
    _patch_identity(monkeypatch)
    stored = _stored("order-1")
    bridge = FakeBridge((stored,))
    integration = host_integration.HostAutoOfferIntegration(bridge)

    assert integration.next_purchase_result(
        [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}]
    ) is AutoOfferResult.BLOCKED
    assert bridge.steps == []


def test_recovery_hard_bound_blocks_buggy_persisted_cycle(monkeypatch):
    _patch_identity(monkeypatch)
    initial = _stored(
        "order-1",
        status=DeliveryStatus.OFFER_CONFIRMED,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        revision=1,
    )

    class BuggyBridge(FakeBridge):
        def __init__(self, current):
            super().__init__((current,))

        def step(self, delivery):
            self.steps.append((delivery.snapshot.buff_order_id, delivery.snapshot.delivery_status))
            after = StoredDelivery(
                snapshot=replace(
                    delivery.snapshot,
                    delivery_error=f"bug-{delivery.revision + 1}",
                ),
                revision=delivery.revision + 1,
            )
            self.current[delivery.snapshot.buff_order_id] = after
            return SimpleNamespace(
                after=after,
                persisted=True,
                decision=SimpleNamespace(result=AutoOfferResult.WAITING),
            )

    bridge = BuggyBridge(initial)
    integration = host_integration.HostAutoOfferIntegration(bridge)
    assert integration.next_purchase_result([_host_row("order-1")]) is AutoOfferResult.BLOCKED
    assert len(bridge.steps) == host_integration._MAX_RECOVERY_STEPS_PER_DELIVERY


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
        "_run_auto_offer_receive_once",
        lambda *_args: AutoOfferResult.WAITING,
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
