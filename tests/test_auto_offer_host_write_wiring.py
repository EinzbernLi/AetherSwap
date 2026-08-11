from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.auto_offer.host_integration as host_integration
from app.auto_offer.adapters import PlatformCapability
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.store import AutoOfferStore, StoredDelivery
from app.services.buff_client import BuffClient


ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000001"
COOKIE = f"steamLoginSecure={STEAM_ID}%7C%7Ctoken"


def _delivery(
    order_id: str,
    *,
    status: DeliveryStatus = DeliveryStatus.PENDING_DIRECTION,
    mode: DeliveryMode | None = None,
    revision: int = 1,
) -> StoredDelivery:
    attempted_at = None
    sent_at = None
    tradeoffer_id = None
    error = None
    if status is DeliveryStatus.RESULT_UNKNOWN:
        attempted_at = 10.0
        error = "write_result_unknown"
    if status is DeliveryStatus.OFFER_SENT:
        attempted_at = 10.0
        sent_at = 11.0
        tradeoffer_id = f"offer-{order_id}"
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=tradeoffer_id,
            offer_attempted_at=attempted_at,
            offer_sent_at=sent_at,
            received_at=None,
            delivery_error=error,
            pending_receipt=True,
            assetid=None,
        ),
        revision=revision,
    )


def _purchase(order_id: str) -> dict:
    return {
        "buff_order_id": order_id,
        "pending_receipt": True,
        "assetid": None,
    }


def _host_rows(*order_ids: str) -> list[dict]:
    return [_purchase(order_id) for order_id in order_ids]


def _patch_identity(monkeypatch) -> None:
    monkeypatch.setattr(host_integration, "get_current_id", lambda: ACCOUNT_ID)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": STEAM_ID, "cookies": COOKIE},
    )


class ScriptedBridge:
    def __init__(self, *, fresh_orders=(), transitions=None):
        self.account_id = ACCOUNT_ID
        self.recipient_steam_id = STEAM_ID
        self.fresh_orders = set(fresh_orders)
        self.transitions = dict(transitions or {})
        self.current = {}
        self.events = []
        self.closed = False

    def register_committed_purchase(self, record):
        order_id = record["buff_order_id"]
        self.events.append(("register", order_id))
        delivery = self.current.setdefault(order_id, _delivery(order_id))
        return delivery if order_id in self.fresh_orders else None

    def step(self, delivery):
        order_id = delivery.snapshot.buff_order_id
        status = delivery.snapshot.delivery_status
        self.events.append(("step", order_id, status))
        key = (order_id, status)
        after = self.transitions.get(key, delivery)
        self.current[order_id] = after
        return SimpleNamespace(after=after)

    def list_recoverable(self):
        return tuple(self.current.values())

    def close(self):
        self.events.append(("close",))
        self.closed = True


def test_store_created_flag_is_atomic_and_historical_api_is_preserved(tmp_path):
    store = AutoOfferStore(tmp_path / "auto_offer.db")
    store.initialize()
    initial = _delivery("order-1").snapshot

    first, created = store.ensure_initial_with_created(initial)
    duplicate, duplicate_created = store.ensure_initial_with_created(initial)
    historical = store.ensure_initial(initial)

    assert created is True
    assert duplicate_created is False
    assert first == duplicate == historical
    assert first.revision == 1
    store.close()


def test_buff_client_facade_owns_wait_send_read_and_single_send(monkeypatch):
    import buff.buyer_send as buyer_send

    events = []

    class FakeBuyer:
        def get_buy_orders_waiting_to_send_offer(self, game, appid):
            events.append(("wait_send", game, appid))
            return [{"id": "order-1"}]

    fake_buyer = FakeBuyer()
    client = object.__new__(BuffClient)

    def owned_run(operation):
        events.append(("owned_run",))
        return operation(fake_buyer)

    client._run = owned_run

    class FakeTransport:
        def __init__(self, buyer):
            assert buyer is fake_buyer
            events.append(("transport_init",))

        def send(self, **kwargs):
            events.append(("send", kwargs))
            return {"code": "OK"}

    monkeypatch.setattr(buyer_send, "BuffBuyerSendTransport", FakeTransport)

    assert client.get_buy_orders_waiting_to_send_offer("csgo", 730) == [
        {"id": "order-1"}
    ]
    response = client.send_buyer_offer(
        steam_cookie_string=COOKIE,
        buff_order_id="order-1",
        steam_id=STEAM_ID,
        timeout_seconds=15.0,
    )

    assert response == {"code": "OK"}
    assert events == [
        ("owned_run",),
        ("wait_send", "csgo", 730),
        ("owned_run",),
        ("transport_init",),
        (
            "send",
            {
                "steam_cookie_string": COOKIE,
                "buff_order_id": "order-1",
                "steam_id": STEAM_ID,
                "timeout_seconds": 15.0,
            },
        ),
    ]


def test_host_send_transport_uses_only_public_buff_facade_surface():
    calls = []

    class FakeClient:
        def send_buyer_offer(self, **kwargs):
            calls.append(kwargs)
            return {"code": "OK"}

    transport = host_integration._BuffClientBuyerSendTransport(FakeClient())
    result = transport.send(
        steam_cookie_string=COOKIE,
        buff_order_id="order-1",
        steam_id=STEAM_ID,
        timeout_seconds=15.0,
    )

    assert result == {"code": "OK"}
    assert calls == [
        {
            "steam_cookie_string": COOKIE,
            "buff_order_id": "order-1",
            "steam_id": STEAM_ID,
            "timeout_seconds": 15.0,
        }
    ]
    source = open(host_integration.__file__, encoding="utf-8").read()
    assert "._run(" not in source
    assert "._buyer" not in source
    assert "_make_request(" not in source


def test_preexisting_duplicate_never_authorizes_first_send(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(fresh_orders=())
    integration = host_integration.HostAutoOfferIntegration(bridge)

    integration.register_committed_purchase(_purchase("order-1"))
    result = integration.next_purchase_result(_host_rows("order-1"))

    assert result is AutoOfferResult.WAITING
    assert bridge.events == [("register", "order-1")]


def test_runtime_identity_mismatch_blocks_before_any_platform_step(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(fresh_orders=("order-1",))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))

    monkeypatch.setattr(host_integration, "get_current_id", lambda: "account-2")
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": STEAM_ID},
    )

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.BLOCKED
    assert bridge.events == [("register", "order-1")]


def test_batch_registration_finishes_before_any_platform_step(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    transitions = {
        ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-1",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.SELLER_SENDS_OFFER,
            revision=2,
        ),
        ("order-2", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-2",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.SELLER_SENDS_OFFER,
            revision=2,
        ),
    }
    bridge = ScriptedBridge(
        fresh_orders=("order-1", "order-2"),
        transitions=transitions,
    )
    integration = host_integration.HostAutoOfferIntegration(bridge)

    integration.register_committed_purchase(_purchase("order-1"))
    integration.register_committed_purchase(_purchase("order-2"))
    assert bridge.events == [("register", "order-1"), ("register", "order-2")]

    result = integration.next_purchase_result(_host_rows("order-1", "order-2"))

    assert result is AutoOfferResult.WAITING
    assert bridge.events[:2] == [("register", "order-1"), ("register", "order-2")]
    assert bridge.events[2:] == [
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
        ("step", "order-2", DeliveryStatus.PENDING_DIRECTION),
    ]


def test_buyer_first_send_is_one_shot_then_exact_recovery(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    transitions = {
        ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-1",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=2,
        ),
        ("order-1", DeliveryStatus.AWAITING_OFFER): _delivery(
            "order-1",
            status=DeliveryStatus.RESULT_UNKNOWN,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=4,
        ),
        ("order-1", DeliveryStatus.RESULT_UNKNOWN): _delivery(
            "order-1",
            status=DeliveryStatus.OFFER_SENT,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=5,
        ),
    }
    bridge = ScriptedBridge(fresh_orders=("order-1",), transitions=transitions)
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
        ("step", "order-1", DeliveryStatus.AWAITING_OFFER),
        ("step", "order-1", DeliveryStatus.RESULT_UNKNOWN),
    ]

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    assert bridge.events[-1] == ("step", "order-1", DeliveryStatus.RESULT_UNKNOWN)
    assert len(bridge.events) == 4


def test_seller_direction_never_enters_buyer_send_step(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(
        fresh_orders=("order-1",),
        transitions={
            ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
                "order-1",
                status=DeliveryStatus.AWAITING_OFFER,
                mode=DeliveryMode.SELLER_SENDS_OFFER,
                revision=2,
            )
        },
    )
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
    ]


def test_unresolved_write_stops_later_fresh_first_sends(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    transitions = {
        ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-1",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=2,
        ),
        ("order-1", DeliveryStatus.AWAITING_OFFER): _delivery(
            "order-1",
            status=DeliveryStatus.RESULT_UNKNOWN,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=4,
        ),
        ("order-1", DeliveryStatus.RESULT_UNKNOWN): _delivery(
            "order-1",
            status=DeliveryStatus.RESULT_UNKNOWN,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=4,
        ),
        ("order-2", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-2",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=2,
        ),
    }
    bridge = ScriptedBridge(
        fresh_orders=("order-1", "order-2"),
        transitions=transitions,
    )
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))
    integration.register_committed_purchase(_purchase("order-2"))

    assert integration.next_purchase_result(_host_rows("order-1", "order-2")) is AutoOfferResult.WAITING
    assert ("step", "order-2", DeliveryStatus.PENDING_DIRECTION) not in bridge.events
    assert bridge.events[-1] == ("step", "order-1", DeliveryStatus.RESULT_UNKNOWN)


def test_unresolved_checkout_defers_first_send_and_close_does_not_write(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration,
        "get_unresolved_checkout",
        lambda: {"stage": "batch_partial"},
    )
    bridge = ScriptedBridge(fresh_orders=("order-1",))
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    integration.close()

    assert bridge.events == [("register", "order-1"), ("close",)]
    assert bridge.closed is True


def test_close_dispatches_final_resolved_checkout_once(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(
        fresh_orders=("order-1",),
        transitions={
            ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
                "order-1",
                status=DeliveryStatus.AWAITING_OFFER,
                mode=DeliveryMode.SELLER_SENDS_OFFER,
                revision=2,
            )
        },
    )
    integration = host_integration.HostAutoOfferIntegration(bridge)
    integration.register_committed_purchase(_purchase("order-1"))

    integration.close()
    integration.close()

    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
        ("close",),
    ]


def test_execution_time_steam_identity_is_rechecked(monkeypatch):
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {
            "steam_id": "76561198000000002",
            "cookies": COOKIE,
        },
    )
    with pytest.raises(host_integration.HostAutoOfferIntegrationError):
        host_integration._steam_cookie_for_expected(STEAM_ID)


def test_active_builder_wires_one_write_enabled_coordinator_without_platform_io(
    monkeypatch, tmp_path
):
    calls = []

    class FakeSession:
        verify = True

        def close(self):
            calls.append(("session_close",))

    class FakeStore:
        def __init__(self, path):
            calls.append(("store", path))

        def initialize(self):
            calls.append(("store_initialize",))

        def close(self):
            calls.append(("store_close",))

    class FakeReader:
        bound_account_steam_id = STEAM_ID

        def __init__(self, *_args, **_kwargs):
            calls.append(("reader",))

    class FakeAdapter:
        def __init__(self, *_args, **_kwargs):
            calls.append(("adapter",))

    class FakeSendAdapter:
        def __init__(self, transport, **kwargs):
            calls.append(("send_adapter", transport, kwargs))

    class FakeCoordinator:
        def __init__(self, store, adapters, **kwargs):
            calls.append(("coordinator", store, dict(adapters), kwargs))

        def step(self, _delivery):
            raise AssertionError("builder performed platform execution")

    class FakeClient:
        def send_buyer_offer(self, **_kwargs):
            raise AssertionError("builder performed buyer-send execution")

    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration.requests, "Session", FakeSession)
    monkeypatch.setattr(host_integration, "AutoOfferStore", FakeStore)
    monkeypatch.setattr(host_integration, "SteamTradeOfferHttpReader", FakeReader)
    monkeypatch.setattr(host_integration, "SteamCompletedTradeHttpReader", FakeReader)
    monkeypatch.setattr(host_integration, "BuffReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "SteamTradeOfferReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "SteamCompletedTradeReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "BuffBuyerSendOfferAdapter", FakeSendAdapter)
    monkeypatch.setattr(host_integration, "DeliveryCoordinator", FakeCoordinator)

    client = FakeClient()
    bridge = host_integration._build_active_host_auto_offer_bridge(
        buff_client=client,
        account_id=ACCOUNT_ID,
        account_steam_id=STEAM_ID,
        store_path=tmp_path / "auto_offer.db",
    )

    coordinator_calls = [call for call in calls if call[0] == "coordinator"]
    assert len(coordinator_calls) == 1
    registry = coordinator_calls[0][2]
    kwargs = coordinator_calls[0][3]
    assert set(registry) == {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        PlatformCapability.SEND_OFFER,
    }
    assert kwargs["allow_writes"] is True
    send_calls = [call for call in calls if call[0] == "send_adapter"]
    assert len(send_calls) == 1
    assert isinstance(send_calls[0][1], host_integration._BuffClientBuyerSendTransport)
    bridge.close()
