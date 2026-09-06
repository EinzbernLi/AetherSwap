from __future__ import annotations

import threading

import pytest

import app.auto_offer.canary_receive as receive
from app.auto_offer.canary_receive import (
    CanaryReceiveTarget,
    CanaryReceiveTickError,
    run_receive_owned_canary_tick,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.host_integration import DeliveryTickOutcome
from app.auto_offer.runtime_mode import (
    AutoOfferRuntimeMode,
    AutoOfferRuntimeState,
)
from app.auto_offer.store import StoredDelivery


ORDER = "order-100"
ACCOUNT = "account-100"
STEAM = "76561198000000100"


def _target() -> CanaryReceiveTarget:
    return CanaryReceiveTarget(
        host_db_id=100,
        buff_order_id=ORDER,
        purchase_id=f"buff:{ORDER}",
        account_id=ACCOUNT,
        recipient_steam_id=STEAM,
    )


def _host() -> list[dict]:
    return [
        {
            "_db_id": 100,
            "buff_order_id": ORDER,
            "pending_receipt": True,
            "assetid": None,
        }
    ]


def _stored(
    status: DeliveryStatus = DeliveryStatus.AWAITING_OFFER,
    *,
    mode: DeliveryMode = DeliveryMode.BUYER_SENDS_OFFER,
    order_id: str = ORDER,
    revision: int = 2,
) -> StoredDelivery:
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id=ACCOUNT,
            recipient_steam_id=STEAM,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=(
                "write_result_unknown"
                if status is DeliveryStatus.RESULT_UNKNOWN
                else None
            ),
            pending_receipt=True,
            assetid=None,
            counterparty_steam_id=None,
        ),
        revision=revision,
    )


class _Client:
    def __init__(self, events):
        self.events = events
        self.created_thread = threading.get_ident()
        self.closed_thread = None

    def close(self):
        self.events.append("client-close")
        self.closed_thread = threading.get_ident()


class _Integration:
    account_id = ACCOUNT
    recipient_steam_id = STEAM

    def __init__(
        self,
        events,
        *,
        stored=None,
        recoverable=None,
        result=AutoOfferResult.WAITING,
        close_raises=False,
    ):
        self.events = events
        self.stored = stored or _stored()
        self.recoverable = (
            tuple(recoverable)
            if recoverable is not None
            else (self.stored,)
        )
        self.result = result
        self.close_raises = close_raises
        self.created_thread = threading.get_ident()
        self.closed_thread = None
        self.delivery_calls = 0

    def get_by_purchase_id(self, purchase_id):
        if purchase_id == self.stored.snapshot.purchase_id:
            return self.stored
        return None

    def list_recoverable(self):
        return self.recoverable

    def run_delivery_tick(self, host_purchases, *, cursor=None):
        self.events.append("delivery")
        self.delivery_calls += 1
        return DeliveryTickOutcome(
            self.result,
            ORDER,
            (ORDER,),
        )

    def close(self):
        self.events.append("integration-close")
        self.closed_thread = threading.get_ident()
        if self.close_raises:
            raise RuntimeError("close")


class _State:
    @staticmethod
    def complete_purchase_receipt_by_id(*_args):
        return True

    @staticmethod
    def delete_refund_cleanup_purchase(*_args):
        return True


def _install_common(
    monkeypatch,
    *,
    runtime_mode=AutoOfferRuntimeMode.ON,
    requested_enabled=True,
    active_delivery_count=1,
    checkout=None,
    credentials=None,
    integration=None,
):
    import app.config_loader as config_loader
    import app.services.buff_checkout_guard as checkout_guard
    import app.services.buff_client as buff_client_module
    import app.state as state_module

    events = []
    config = {
        "auto_offer": {"enabled": requested_enabled},
        "buff": {"pay_method": "wechat"},
    }
    creds = (
        {
            "cookies": "fake-cookie",
            "generation": 7,
            "user_agent": "ua",
        }
        if credentials is None
        else credentials
    )
    runtime = AutoOfferRuntimeState(
        requested_enabled=requested_enabled,
        active_delivery_count=active_delivery_count,
        mode=runtime_mode,
    )
    built_clients = []
    built_integrations = []
    seen_credentials = []

    monkeypatch.setattr(
        config_loader,
        "load_app_config_validated",
        lambda: config,
    )
    monkeypatch.setattr(
        config_loader,
        "get_buff_credentials",
        lambda: dict(creds),
    )
    monkeypatch.setattr(
        checkout_guard,
        "get_unresolved_checkout",
        lambda: checkout,
    )

    def build_client(current, cfg):
        events.append("client-build")
        seen_credentials.append(dict(current))
        assert cfg == config
        client = _Client(events)
        built_clients.append(client)
        return client

    monkeypatch.setattr(
        buff_client_module,
        "create_buff_client_from_config",
        build_client,
    )
    monkeypatch.setattr(
        state_module,
        "get_state",
        lambda: _State(),
    )
    monkeypatch.setattr(
        receive,
        "_runtime_for_target",
        lambda _config, _host: runtime,
    )

    selected = integration or _Integration(events)

    def build_integration(**kwargs):
        events.append("integration-build")
        assert kwargs["buff_client"] is built_clients[-1]
        assert kwargs["runtime_state"] is runtime
        built_integrations.append(selected)
        return selected

    monkeypatch.setattr(
        receive,
        "build_host_auto_offer_integration",
        build_integration,
    )
    return {
        "events": events,
        "runtime": runtime,
        "integration": selected,
        "clients": built_clients,
        "integrations": built_integrations,
        "seen_credentials": seen_credentials,
    }


def test_receive_tick_builds_and_closes_client_and_integration_in_calling_thread(
    monkeypatch,
):
    env = _install_common(monkeypatch)
    thread_id = threading.get_ident()

    result = run_receive_owned_canary_tick(_target(), _host())

    assert result.outcome.result is AutoOfferResult.WAITING
    assert env["events"] == [
        "client-build",
        "integration-build",
        "delivery",
        "integration-close",
        "client-close",
    ]
    client = env["clients"][0]
    integration = env["integration"]
    assert client.created_thread == thread_id
    assert client.closed_thread == thread_id
    assert integration.created_thread == thread_id
    assert integration.closed_thread == thread_id


def test_receive_tick_uses_latest_credential_generation_when_provisioning_client(
    monkeypatch,
):
    env = _install_common(
        monkeypatch,
        credentials={
            "cookies": "rotated-cookie",
            "generation": 19,
            "user_agent": "rotated-ua",
        },
    )

    run_receive_owned_canary_tick(_target(), _host())

    assert env["seen_credentials"] == [
        {
            "cookies": "rotated-cookie",
            "generation": 19,
            "user_agent": "rotated-ua",
        }
    ]


def test_receive_tick_allows_exact_target_to_drain_after_intent_off(monkeypatch):
    env = _install_common(
        monkeypatch,
        runtime_mode=AutoOfferRuntimeMode.DRAINING,
        requested_enabled=False,
        active_delivery_count=1,
    )

    result = run_receive_owned_canary_tick(_target(), _host())

    assert result.runtime_mode is AutoOfferRuntimeMode.DRAINING
    assert result.outcome.result is AutoOfferResult.WAITING
    assert env["integration"].delivery_calls == 1


def test_blocked_runtime_fails_before_client_or_delivery(monkeypatch):
    import app.config_loader as config_loader
    import app.services.buff_checkout_guard as checkout_guard
    import app.services.buff_client as buff_client_module

    config = {"auto_offer": {"enabled": True}}
    monkeypatch.setattr(
        config_loader,
        "load_app_config_validated",
        lambda: config,
    )
    monkeypatch.setattr(
        checkout_guard,
        "get_unresolved_checkout",
        lambda: None,
    )
    monkeypatch.setattr(
        buff_client_module,
        "create_buff_client_from_config",
        lambda *_args, **_kwargs: pytest.fail(
            "blocked runtime must stop before BUFF client build"
        ),
    )

    def blocked(_config, _host):
        raise CanaryReceiveTickError("canary_runtime_blocked")

    monkeypatch.setattr(receive, "_runtime_for_target", blocked)

    with pytest.raises(
        CanaryReceiveTickError,
        match="canary_runtime_blocked",
    ):
        run_receive_owned_canary_tick(_target(), _host())


def test_checkout_ambiguity_fails_before_client_build(monkeypatch):
    import app.config_loader as config_loader
    import app.services.buff_checkout_guard as checkout_guard
    import app.services.buff_client as buff_client_module

    monkeypatch.setattr(
        checkout_guard,
        "get_unresolved_checkout",
        lambda: {"stage": "write_result_unknown"},
    )
    monkeypatch.setattr(
        config_loader,
        "load_app_config_validated",
        lambda: pytest.fail("checkout ambiguity must stop before config/client"),
    )
    monkeypatch.setattr(
        buff_client_module,
        "create_buff_client_from_config",
        lambda *_args, **_kwargs: pytest.fail(
            "checkout ambiguity must stop before client build"
        ),
    )

    with pytest.raises(
        CanaryReceiveTickError,
        match="canary_checkout_unresolved",
    ):
        run_receive_owned_canary_tick(_target(), _host())


def test_unrelated_recoverable_store_row_fails_before_delivery(monkeypatch):
    events = []
    target = _stored()
    unrelated = _stored(order_id="other-order")
    integration = _Integration(
        events,
        stored=target,
        recoverable=(target, unrelated),
    )
    env = _install_common(
        monkeypatch,
        integration=integration,
    )
    integration.events = env["events"]

    with pytest.raises(
        CanaryReceiveTickError,
        match="canary_store_target_not_exclusive",
    ):
        run_receive_owned_canary_tick(_target(), _host())

    assert integration.delivery_calls == 0
    assert env["events"][-2:] == [
        "integration-close",
        "client-close",
    ]


def test_result_unknown_never_dispatches_another_delivery_write(monkeypatch):
    events = []
    unknown = _stored(
        DeliveryStatus.RESULT_UNKNOWN,
        revision=5,
    )
    integration = _Integration(events, stored=unknown)
    env = _install_common(
        monkeypatch,
        integration=integration,
    )
    integration.events = env["events"]

    result = run_receive_owned_canary_tick(_target(), _host())

    assert result.outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert result.reason == "canary_result_unknown"
    assert integration.delivery_calls == 0


def test_runtime_identity_drift_fails_before_delivery(monkeypatch):
    class DriftIntegration(_Integration):
        account_id = "different-account"

    events = []
    integration = DriftIntegration(events)
    env = _install_common(
        monkeypatch,
        integration=integration,
    )
    integration.events = env["events"]

    with pytest.raises(
        CanaryReceiveTickError,
        match="canary_runtime_identity_mismatch",
    ):
        run_receive_owned_canary_tick(_target(), _host())

    assert integration.delivery_calls == 0


def test_normal_delivery_blocked_has_stable_secret_free_reason(monkeypatch):
    events = []
    integration = _Integration(
        events,
        result=AutoOfferResult.BLOCKED,
    )
    env = _install_common(
        monkeypatch,
        integration=integration,
    )
    integration.events = env["events"]

    result = run_receive_owned_canary_tick(_target(), _host())

    assert result.outcome.result is AutoOfferResult.BLOCKED
    assert result.reason == "canary_platform_or_state_blocked"


def test_integration_close_failure_is_stable_and_client_still_closes(
    monkeypatch,
):
    events = []
    integration = _Integration(
        events,
        close_raises=True,
    )
    env = _install_common(
        monkeypatch,
        integration=integration,
    )
    integration.events = env["events"]

    with pytest.raises(
        CanaryReceiveTickError,
        match="canary_receive_integration_close_failed",
    ):
        run_receive_owned_canary_tick(_target(), _host())

    assert env["events"][-2:] == [
        "integration-close",
        "client-close",
    ]
    assert env["clients"][0].closed_thread == threading.get_ident()
