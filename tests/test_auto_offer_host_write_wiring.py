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
IDENTITY_SECRET = "eHh4eHh4eHh4eHh4eHh4eHh4eHg="


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
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        attempted_at = 10.0
    if status is DeliveryStatus.RESULT_UNKNOWN:
        attempted_at = 10.0
        error = "write_result_unknown"
    if status in {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    } and mode is DeliveryMode.BUYER_SENDS_OFFER:
        attempted_at = 10.0
        sent_at = 11.0
        tradeoffer_id = f"offer-{order_id}"
    if status in {
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    } and mode is DeliveryMode.SELLER_SENDS_OFFER:
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


def _purchase(order_id: str, *, db_id: int = 1) -> dict:
    return {
        "_db_id": db_id,
        "buff_order_id": order_id,
        "pending_receipt": True,
        "assetid": None,
    }


def _host_rows(*order_ids: str) -> list[dict]:
    return [
        _purchase(order_id, db_id=index + 1)
        for index, order_id in enumerate(order_ids)
    ]


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
        lambda: {
            "steam_id": STEAM_ID,
            "cookies": COOKIE,
            "identity_secret": IDENTITY_SECRET,
        },
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
        return SimpleNamespace(
            after=after,
            persisted=after != delivery,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def recover_result_unknown_readonly(self, delivery):
        self.events.append(
            (
                "recover_result_unknown_readonly",
                delivery.snapshot.buff_order_id,
                delivery.snapshot.delivery_status,
            )
        )
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def list_recoverable(self):
        return tuple(
            delivery
            for delivery in self.current.values()
            if delivery.snapshot.delivery_status is not DeliveryStatus.RECEIVED
        )

    def get_by_purchase_id(self, purchase_id):
        for delivery in self.current.values():
            if delivery.snapshot.purchase_id == purchase_id:
                return delivery
        return None

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


def test_buff_client_history_facade_delegates_once_preserving_result_and_exception():
    payload = {"code": "OK", "data": {"page_num": 2}}
    calls = []

    class FakeBuyer:
        def get_buy_order_history_page(self, page_num, game):
            calls.append((page_num, game))
            return payload

    fake_buyer = FakeBuyer()
    client = object.__new__(BuffClient)
    run_calls = []

    def owned_run(operation):
        run_calls.append(operation)
        return operation(fake_buyer)

    client._run = owned_run
    before = dict(client.__dict__)

    assert client.get_buy_order_history_page(2, "csgo") is payload
    assert calls == [(2, "csgo")]
    assert len(run_calls) == 1
    assert client.__dict__ == before
    assert payload == {"code": "OK", "data": {"page_num": 2}}

    error_calls = []

    class RaisingBuyer:
        def get_buy_order_history_page(self, page_num, game):
            error_calls.append((page_num, game))
            raise RuntimeError("history failure")

    raising_buyer = RaisingBuyer()
    error_client = object.__new__(BuffClient)
    error_run_calls = []

    def owned_error_run(operation):
        error_run_calls.append(operation)
        return operation(raising_buyer)

    error_client._run = owned_error_run
    error_before = dict(error_client.__dict__)

    with pytest.raises(RuntimeError, match="history failure"):
        error_client.get_buy_order_history_page(3, "csgo")

    assert error_calls == [(3, "csgo")]
    assert len(error_run_calls) == 1
    assert error_client.__dict__ == error_before


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
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )

    integration.register_committed_purchase(_purchase("order-1"))
    result = integration.next_purchase_result(_host_rows("order-1"))

    assert result is AutoOfferResult.WAITING
    assert integration._fresh_deliveries == []
    assert bridge.events == [("register", "order-1")]


def test_preexisting_buyer_awaiting_offer_is_never_sent(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(fresh_orders=())
    bridge.current["order-1"] = _delivery(
        "order-1",
        status=DeliveryStatus.AWAITING_OFFER,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
        revision=2,
    )
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    assert bridge.events == []


def test_runtime_identity_mismatch_blocks_before_any_platform_step(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge(fresh_orders=("order-1",))
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
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
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )

    integration.register_committed_purchase(_purchase("order-1"))
    integration.register_committed_purchase(_purchase("order-2"))
    assert bridge.events == [("register", "order-1"), ("register", "order-2")]

    result = integration.next_purchase_result(_host_rows("order-1", "order-2"))

    assert result is AutoOfferResult.WAITING
    assert integration._fresh_deliveries == []
    assert bridge.events == [("register", "order-1"), ("register", "order-2")]

    outcome = integration.run_delivery_tick(_host_rows("order-1", "order-2"))

    assert outcome.visited_order_ids == ("order-1", "order-2")
    assert bridge.events[2:] == [
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
        ("step", "order-2", DeliveryStatus.PENDING_DIRECTION),
    ]


def test_buyer_awaiting_offer_is_not_authorized_to_send_from_persisted_state(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    transitions = {
        ("order-1", DeliveryStatus.PENDING_DIRECTION): _delivery(
            "order-1",
            status=DeliveryStatus.AWAITING_OFFER,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=2,
        ),
    }
    bridge = ScriptedBridge(fresh_orders=("order-1",), transitions=transitions)
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
    integration.register_committed_purchase(_purchase("order-1"))

    rows = _host_rows("order-1")
    assert integration.next_purchase_result(rows) is AutoOfferResult.WAITING
    assert bridge.events == [("register", "order-1")]

    first = integration.run_delivery_tick(rows)
    assert first.visited_order_ids == ("order-1",)
    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
    ]

    second = integration.run_delivery_tick(rows, cursor=first.next_cursor)
    assert second.visited_order_ids == ("order-1",)
    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
    ]


def test_seller_direction_progression_belongs_to_delivery_tick(monkeypatch):
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
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
    integration.register_committed_purchase(_purchase("order-1"))

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    assert bridge.events == [("register", "order-1")]

    outcome = integration.run_delivery_tick(_host_rows("order-1"))

    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.events == [
        ("register", "order-1"),
        ("step", "order-1", DeliveryStatus.PENDING_DIRECTION),
    ]


def test_result_unknown_globally_stops_normal_runtime_progression(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(host_integration, "get_unresolved_checkout", lambda: None)
    bridge = ScriptedBridge()
    bridge.current = {
        "order-1": _delivery(
            "order-1",
            status=DeliveryStatus.RESULT_UNKNOWN,
            mode=DeliveryMode.BUYER_SENDS_OFFER,
            revision=4,
        ),
        "order-2": _delivery("order-2"),
    }
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
    rows = _host_rows("order-1", "order-2")

    assert integration.next_purchase_result(rows) is AutoOfferResult.RESULT_UNKNOWN
    outcome = integration.run_delivery_tick(rows)

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert outcome.visited_order_ids == ("order-1",)
    assert bridge.events == [
        (
            "recover_result_unknown_readonly",
            "order-1",
            DeliveryStatus.RESULT_UNKNOWN,
        )
    ]


def test_unresolved_checkout_defers_all_fresh_platform_steps_and_close_does_not_write(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        host_integration,
        "get_unresolved_checkout",
        lambda: {"stage": "batch_partial"},
    )
    bridge = ScriptedBridge(fresh_orders=("order-1",))
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
    integration.register_committed_purchase(_purchase("order-1"))

    assert integration.next_purchase_result(_host_rows("order-1")) is AutoOfferResult.WAITING
    integration.close()

    assert bridge.events == [("register", "order-1"), ("close",)]
    assert bridge.closed is True


def test_close_never_dispatches_even_after_checkout_is_resolved(monkeypatch):
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
    integration = host_integration.HostAutoOfferIntegration(
        bridge, delete_refund_cleanup_purchase=lambda *_args: True
    )
    integration.register_committed_purchase(_purchase("order-1"))

    integration.close()
    integration.close()

    assert bridge.events == [
        ("register", "order-1"),
        ("close",),
    ]


def test_execution_time_steam_identity_is_rechecked(monkeypatch):
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {
            "steam_id": "76561198000000002",
            "cookies": COOKIE,
            "identity_secret": IDENTITY_SECRET,
        },
    )
    with pytest.raises(host_integration.HostAutoOfferIntegrationError):
        host_integration._steam_cookie_for_expected(STEAM_ID)


def test_missing_confirmation_identity_secret_fails_before_session_or_platform_construction(monkeypatch, tmp_path):
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": STEAM_ID, "cookies": COOKIE},
    )
    monkeypatch.setattr(
        host_integration.requests,
        "Session",
        lambda: (_ for _ in ()).throw(AssertionError("session constructed")),
    )

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="steam_identity_secret_required",
    ):
        host_integration._build_active_host_auto_offer_bridge(
            buff_client=object(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            store_path=tmp_path / "auto_offer.db",
        )


def test_malformed_confirmation_identity_secret_fails_closed_without_request(monkeypatch, tmp_path):
    calls = []

    class FakeSession:
        verify = True

        def close(self):
            calls.append(("session_close",))

        def get(self, *_args, **_kwargs):
            raise AssertionError("GET executed")

        def post(self, *_args, **_kwargs):
            raise AssertionError("POST executed")

    class FakeStore:
        def __init__(self, _path):
            calls.append(("store",))

        def initialize(self):
            calls.append(("store_initialize",))

        def close(self):
            calls.append(("store_close",))

    class FakeReader:
        bound_account_steam_id = STEAM_ID

        def __init__(self, *_args, **_kwargs):
            calls.append(("reader",))

    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {
            "steam_id": STEAM_ID,
            "cookies": COOKIE,
            "identity_secret": "not-base64",
        },
    )
    monkeypatch.setattr(host_integration.requests, "Session", FakeSession)
    monkeypatch.setattr(host_integration, "AutoOfferStore", FakeStore)
    monkeypatch.setattr(host_integration, "SteamTradeOfferHttpReader", FakeReader)
    monkeypatch.setattr(host_integration, "SteamCompletedTradeHttpReader", FakeReader)

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="auto_offer_bridge_build_failed",
    ) as exc_info:
        host_integration._build_active_host_auto_offer_bridge(
            buff_client=object(),
            account_id=ACCOUNT_ID,
            account_steam_id=STEAM_ID,
            store_path=tmp_path / "auto_offer.db",
        )
    assert "not-base64" not in repr(exc_info.value)
    assert all(call[0] not in {"get", "post"} for call in calls)


def test_active_builder_wires_exact_confirmation_stack_without_platform_io(
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

    class FakeConfirmationTransport:
        bound_account_steam_id = STEAM_ID

        def __init__(self, cookie, identity_secret, *, session, timeout):
            calls.append(
                (
                    "confirmation_transport",
                    cookie,
                    identity_secret,
                    session,
                    timeout,
                )
            )

        def confirm(self, *_args, **_kwargs):
            raise AssertionError("builder performed confirmation execution")

    class FakeAcceptTransport:
        bound_account_steam_id = STEAM_ID

        def __init__(self, cookie, *, session):
            calls.append(("accept_transport", cookie, session))

        def accept(self, *_args, **_kwargs):
            raise AssertionError("builder performed accept execution")

    class FakeAdapter:
        def __init__(self, *_args, **_kwargs):
            calls.append(("adapter",))

    class FakeSendAdapter:
        def __init__(self, transport, **kwargs):
            calls.append(("send_adapter", transport, kwargs))

    class FakeConfirmationAdapter:
        def __init__(self, transport, **kwargs):
            calls.append(("confirmation_adapter", transport, kwargs))

    class FakeAcceptAdapter:
        def __init__(self, transport, **kwargs):
            calls.append(("accept_adapter", transport, kwargs))

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
    monkeypatch.setattr(
        host_integration,
        "SteamTradeOfferConfirmationTransport",
        FakeConfirmationTransport,
    )
    monkeypatch.setattr(
        host_integration,
        "SteamIncomingOfferAcceptTransport",
        FakeAcceptTransport,
    )
    monkeypatch.setattr(host_integration, "BuffReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "SteamTradeOfferReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "SteamCompletedTradeReadOnlyAdapter", FakeAdapter)
    monkeypatch.setattr(host_integration, "BuffBuyerSendOfferAdapter", FakeSendAdapter)
    monkeypatch.setattr(
        host_integration,
        "SteamTradeOfferConfirmationAdapter",
        FakeConfirmationAdapter,
    )
    monkeypatch.setattr(
        host_integration,
        "SteamIncomingOfferAcceptAdapter",
        FakeAcceptAdapter,
    )
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
        PlatformCapability.READ_BUYER_SEND_ELIGIBILITY,
        PlatformCapability.READ_BUFF_ORDER_LIFECYCLE,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        PlatformCapability.READ_SELLER_OFFER_ITEM,
        PlatformCapability.SEND_OFFER,
        PlatformCapability.CONFIRM_OFFER,
        PlatformCapability.ACCEPT_OFFER,
    }
    assert kwargs["allow_writes"] is True
    assert kwargs["allow_confirmation_writes"] is True
    assert kwargs["allow_accept_writes"] is True

    send_calls = [call for call in calls if call[0] == "send_adapter"]
    assert len(send_calls) == 1
    assert isinstance(send_calls[0][1], host_integration._BuffClientBuyerSendTransport)

    transport_calls = [call for call in calls if call[0] == "confirmation_transport"]
    assert len(transport_calls) == 1
    assert transport_calls[0][1] == COOKIE
    assert transport_calls[0][2] == IDENTITY_SECRET
    assert transport_calls[0][4] == (
        host_integration._TIMEOUT_SECONDS,
        host_integration._TIMEOUT_SECONDS,
    )
    confirmation_calls = [call for call in calls if call[0] == "confirmation_adapter"]
    assert len(confirmation_calls) == 1
    assert confirmation_calls[0][2] == {
        "account_id": ACCOUNT_ID,
        "recipient_steam_id": STEAM_ID,
    }
    accept_transport_calls = [call for call in calls if call[0] == "accept_transport"]
    assert len(accept_transport_calls) == 1
    assert accept_transport_calls[0][1] == COOKIE
    accept_calls = [call for call in calls if call[0] == "accept_adapter"]
    assert len(accept_calls) == 1
    assert accept_calls[0][2] == {
        "account_id": ACCOUNT_ID,
        "recipient_steam_id": STEAM_ID,
    }
    bridge.close()
