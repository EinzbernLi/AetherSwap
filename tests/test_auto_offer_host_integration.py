from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.auto_offer.host_integration as host_integration
import app.pipeline as pipeline
from app.auto_offer.contracts import AutoOfferResult, DeliverySnapshot, DeliveryStatus
from app.auto_offer.host_integration import HostAutoOfferIntegrationError
from app.pipeline_context import PipelineContext
from app.pipeline_steps import TARGET_REACHED


ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000001"
COOKIE = "steamLoginSecure=fake"


class FakeBridge:
    def __init__(self, recoverable=()):
        self.account_id = ACCOUNT_ID
        self.recipient_steam_id = STEAM_ID
        self.recoverable = tuple(recoverable)
        self.registered = []
        self.closed = False

    def register_committed_purchase(self, record):
        self.registered.append(record)

    def list_recoverable(self):
        return self.recoverable

    def close(self):
        self.closed = True


def _snapshot(order_id: str, *, account_id=ACCOUNT_ID, recipient=STEAM_ID):
    return SimpleNamespace(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id=account_id,
            recipient_steam_id=recipient,
            delivery_mode=None,
            delivery_status=DeliveryStatus.PENDING_DIRECTION,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision=0,
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
    integration = host_integration.build_host_auto_offer_integration(
        config={"auto_offer": {"enabled": True}},
        buff_client=buyer,
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
        )


@pytest.mark.parametrize(
    ("host_rows", "stored", "expected"),
    [
        ([], [], AutoOfferResult.COMPLETE),
        (
            [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}],
            [_snapshot("order-1")],
            AutoOfferResult.WAITING,
        ),
        (
            [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}],
            [],
            AutoOfferResult.BLOCKED,
        ),
        (
            [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}],
            [_snapshot("order-2")],
            AutoOfferResult.BLOCKED,
        ),
        (
            [{"pending_receipt": True, "assetid": None, "buff_order_id": ""}],
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
        FakeBridge([_snapshot("order-1", recipient="76561198000000002")])
    )
    assert integration.next_purchase_result(
        [{"pending_receipt": True, "assetid": None, "buff_order_id": "order-1"}]
    ) is AutoOfferResult.BLOCKED

    _patch_identity(monkeypatch, account_id="account-2")
    assert integration.next_purchase_result([]) is AutoOfferResult.BLOCKED


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

    assert result == (1.0, 1, True)
    assert len(integration.registered) == 1
    assert integration.closed is True
    assert any(message == "AUTO_OFFER_WAITING" for _, message in state.statuses)


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
    import app.services.workers as workers

    class StopWorker(BaseException):
        pass

    sleep_calls = []

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
        lambda: (_ for _ in ()).throw(AssertionError("legacy Steam path used")),
    )
    monkeypatch.setattr(
        workers,
        "get_buff_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("legacy BUFF path used")),
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


def test_task028_write_markers_are_confined_to_host_integration():
    untouched_host_files = [
        Path("app/config_schema.py"),
        Path("app/pipeline.py"),
        Path("app/services/workers.py"),
    ]
    forbidden = (
        "buyer_send_offer",
        "SEND_OFFER",
        "ACCEPT_OFFER",
        ".step(",
        "accept_steam_trade_offer",
    )
    for path in untouched_host_files:
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{marker} found in {path}"

    host_source = Path("app/auto_offer/host_integration.py").read_text(encoding="utf-8")
    for marker in ("DeliveryExecutor", "threading.Thread", "_make_request(", ".execute("):
        assert marker not in host_source, f"{marker} found in host integration"
