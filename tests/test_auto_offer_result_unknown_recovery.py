from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    DeliveryDirectionEvidence,
    OfferStateEvidence,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SendOfferEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from app.auto_offer.coordinator import (
    DeliveryCoordinator,
    ReadOnlyCoordinatorError,
)
from app.auto_offer.platform_readonly import BuffReadOnlyAdapter
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.store import StoredDelivery


IDENTITY = {
    "purchase_id": "purchase-recovery-1",
    "buff_order_id": "buff-order-recovery-1",
    "account_id": "account-recovery-1",
    "recipient_steam_id": "76561198000000001",
}


def make_snapshot(
    status,
    *,
    mode=DeliveryMode.BUYER_SENDS_OFFER,
    steam_tradeoffer_id=None,
    offer_attempted_at=10.0,
    offer_sent_at=None,
    delivery_error=None,
):
    value = DeliverySnapshot(
        **IDENTITY,
        delivery_mode=mode,
        delivery_status=status,
        steam_tradeoffer_id=steam_tradeoffer_id,
        offer_attempted_at=offer_attempted_at,
        offer_sent_at=offer_sent_at,
        received_at=None,
        delivery_error=delivery_error,
        pending_receipt=True,
        assetid=None,
    )
    validate_delivery_snapshot(value)
    return value


def make_delivery(status, *, revision=4, **changes):
    return StoredDelivery(snapshot=make_snapshot(status, **changes), revision=revision)


def request_for(delivery, capability, **changes):
    values = {
        "purchase_id": delivery.snapshot.purchase_id,
        "buff_order_id": delivery.snapshot.buff_order_id,
        "account_id": delivery.snapshot.account_id,
        "recipient_steam_id": delivery.snapshot.recipient_steam_id,
        "revision": delivery.revision,
        "capability": capability,
        "timeout_seconds": 5.0,
        "steam_tradeoffer_id": (
            delivery.snapshot.steam_tradeoffer_id
            if capability
            in {
                PlatformCapability.READ_STEAM_TRADE_OFFER,
                PlatformCapability.READ_STEAM_COMPLETED_TRADE,
            }
            else None
        ),
    }
    values.update(changes)
    return PlatformRequest(**values)


def offer_result(delivery, offer_id="9001"):
    request = request_for(delivery, PlatformCapability.READ_OFFER_STATE)
    return PlatformResult(
        request=request,
        status=PlatformResultStatus.SUCCESS,
        detail="offer_pending",
        evidence=OfferStateEvidence(offer_id, "76561198000000002"),
    )


class SpyStore:
    def __init__(self, current):
        self.current = current
        self.get_calls = []
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id):
        self.get_calls.append(purchase_id)
        return self.current

    def advance(self, current, target):
        self.advance_calls.append((current, target))
        assert current == self.current
        self.current = StoredDelivery(snapshot=target, revision=current.revision + 1)
        return self.current


class SpyAdapter:
    def __init__(self, capability, result_factory):
        self.capabilities = frozenset({capability})
        self._result_factory = result_factory
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return self._result_factory(request)


class HistoryClient:
    def __init__(self, *, history_pages, steam_trades=None):
        self.history_pages = history_pages
        self.steam_trades = [] if steam_trades is None else steam_trades
        self.steam_calls = 0
        self.history_calls = []

    def get_steam_trades(self):
        self.steam_calls += 1
        return self.steam_trades

    def get_buy_order_history_page(self, page_num, game="csgo"):
        self.history_calls.append((page_num, game))
        return self.history_pages.get(page_num)


class NeverSendAdapter:
    capabilities = frozenset({PlatformCapability.SEND_OFFER})

    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="unexpected_send",
            evidence=SendOfferEvidence("should-never-happen"),
        )


def success_offer_adapter(offer_id="9001"):
    return SpyAdapter(
        PlatformCapability.READ_OFFER_STATE,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="offer_pending",
            evidence=OfferStateEvidence(offer_id, "76561198000000002"),
        ),
    )


def unknown_offer_adapter():
    return SpyAdapter(
        PlatformCapability.READ_OFFER_STATE,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.RESULT_UNKNOWN,
            detail="order_not_proven",
        ),
    )


def history_page(page_num=1, total_page=1, items=()):
    return {
        "code": "OK",
        "data": {
            "page_num": page_num,
            "page_size": 10,
            "total_page": total_page,
            "items": list(items),
        },
    }


def history_record(**changes):
    value = {
        "id": IDENTITY["buff_order_id"],
        "buyer_steam_id": IDENTITY["recipient_steam_id"],
        "seller_steam_id": "76561198000000002",
        "tradeofferid": "history-offer-1",
        "state": "SUCCESS",
        "state_text": "已完成",
    }
    value.update(changes)
    return value


def test_planner_recovers_offer_attempted_from_exact_offer_evidence():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)

    decision = plan_read_evidence_transition(
        before,
        offer_result(before),
        observed_at=12.5,
    )

    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == "buyer_offer_recovered"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_SENT
    assert decision.target.steam_tradeoffer_id == "9001"
    assert decision.target.counterparty_steam_id == "76561198000000002"
    assert decision.target.offer_attempted_at == 10.0
    assert decision.target.offer_sent_at == 12.5
    assert decision.target.delivery_error is None


def test_planner_recovers_result_unknown_and_clears_only_write_error():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )

    decision = plan_read_evidence_transition(
        before,
        offer_result(before, "9002"),
        observed_at=14.0,
    )

    assert decision.detail == "buyer_offer_recovered"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_SENT
    assert decision.target.steam_tradeoffer_id == "9002"
    assert decision.target.counterparty_steam_id == "76561198000000002"
    assert decision.target.offer_attempted_at == before.snapshot.offer_attempted_at
    assert decision.target.offer_sent_at == 14.0
    assert decision.target.delivery_error is None
    for field in IDENTITY:
        assert getattr(decision.target, field) == getattr(before.snapshot, field)


@pytest.mark.parametrize("observed_at", [None, True, -1.0, 9.99])
def test_planner_refuses_missing_or_non_monotonic_recovery_time(observed_at):
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)

    decision = plan_read_evidence_transition(
        before,
        offer_result(before),
        observed_at=observed_at,
    )

    assert decision.target is None
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.retryable is False
    assert decision.detail == "recovery_observation_time_invalid"


def test_planner_blocks_forged_identity_before_recovery_semantics():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    forged_request = request_for(
        before,
        PlatformCapability.READ_OFFER_STATE,
        account_id="wrong-account",
    )
    result = PlatformResult(
        request=forged_request,
        status=PlatformResultStatus.SUCCESS,
        detail="offer_pending",
        evidence=OfferStateEvidence("9001", "76561198000000002"),
    )

    decision = plan_read_evidence_transition(before, result, observed_at=12.0)

    assert decision.target is None
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.detail == "identity_mismatch"


def test_planner_does_not_accept_wrong_success_evidence_for_attempted_offer():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    request = request_for(before, PlatformCapability.READ_DELIVERY_DIRECTION)
    result = PlatformResult(
        request=request,
        status=PlatformResultStatus.SUCCESS,
        detail="buyer_sends_offer",
        evidence=DeliveryDirectionEvidence("buyer_sends_offer"),
    )

    decision = plan_read_evidence_transition(before, result, observed_at=12.0)

    assert decision.target is None
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.detail == "evidence_not_allowed"


def test_coordinator_routes_offer_attempted_to_read_and_never_resends():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    store = SpyStore(before)
    read_adapter = success_offer_adapter("9101")
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: 12.0,
    )

    result = coordinator.step(before)

    assert len(read_adapter.calls) == 1
    assert read_adapter.calls[0].capability is PlatformCapability.READ_OFFER_STATE
    assert read_adapter.calls[0].steam_tradeoffer_id is None
    assert send_adapter.calls == []
    assert result.persisted is True
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert result.after.snapshot.steam_tradeoffer_id == "9101"
    assert result.after.snapshot.counterparty_steam_id == "76561198000000002"
    assert result.after.snapshot.offer_sent_at == 12.0
    assert len(store.advance_calls) == 1


def test_coordinator_routes_result_unknown_to_read_and_never_resends():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    store = SpyStore(before)
    read_adapter = success_offer_adapter("9102")
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: 13.0,
    )

    result = coordinator.step(before)

    assert len(read_adapter.calls) == 1
    assert send_adapter.calls == []
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert result.after.snapshot.steam_tradeoffer_id == "9102"
    assert result.after.snapshot.counterparty_steam_id == "76561198000000002"
    assert result.after.snapshot.delivery_error is None


def test_coordinator_result_unknown_history_recovery_binds_exact_offer_without_send():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    store = SpyStore(before)
    client = HistoryClient(
        history_pages={1: history_page(items=[history_record()])},
    )
    read_adapter = BuffReadOnlyAdapter(
        client,
        account_id=IDENTITY["account_id"],
    )
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: 14.0,
    )

    result = coordinator.recover_result_unknown_readonly(before)

    assert result.persisted is True
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert result.after.snapshot.steam_tradeoffer_id == "history-offer-1"
    assert result.after.snapshot.counterparty_steam_id == "76561198000000002"
    assert result.after.snapshot.offer_sent_at == 14.0
    assert result.after.snapshot.delivery_error is None
    assert client.steam_calls == 1
    assert client.history_calls == [(1, "csgo")]
    assert send_adapter.calls == []
    assert len(store.advance_calls) == 1


def test_history_recovery_continues_existing_steam_lifecycle_to_awaiting_inventory():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    store = SpyStore(before)
    client = HistoryClient(
        history_pages={1: history_page(items=[history_record()])},
    )
    buff_adapter = BuffReadOnlyAdapter(
        client,
        account_id=IDENTITY["account_id"],
    )

    def accepted_trade_offer(request):
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="trade_offer_read",
            evidence=SteamTradeOfferEvidence(
                steam_tradeoffer_id=request.steam_tradeoffer_id,
                account_steam_id=IDENTITY["recipient_steam_id"],
                counterparty_steam_id="76561198000000002",
                is_our_offer=True,
                lifecycle=SteamTradeOfferLifecycle.ACCEPTED,
                items_to_give=(),
                items_to_receive=(
                    TradeOfferItemEvidence(
                        appid=730,
                        contextid="2",
                        assetid="asset-source-1",
                        amount=1,
                    ),
                ),
            ),
        )

    steam_adapter = SpyAdapter(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        accepted_trade_offer,
    )
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: buff_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: steam_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: 14.0,
    )

    recovered = coordinator.recover_result_unknown_readonly(before).after
    confirmed = coordinator.step(recovered).after
    awaiting_inventory = coordinator.step(confirmed)

    assert recovered.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert confirmed.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert awaiting_inventory.after.snapshot.delivery_status is (
        DeliveryStatus.AWAITING_INVENTORY
    )
    assert awaiting_inventory.after.snapshot.steam_tradeoffer_id == (
        "history-offer-1"
    )
    assert len(steam_adapter.calls) == 2
    assert client.history_calls == [(1, "csgo")]
    assert send_adapter.calls == []


def test_coordinator_history_recovery_is_not_used_for_seller_awaiting_offer():
    seller_snapshot = DeliverySnapshot(
        **IDENTITY,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
    )
    validate_delivery_snapshot(seller_snapshot)
    before = StoredDelivery(snapshot=seller_snapshot, revision=4)
    store = SpyStore(before)
    client = HistoryClient(
        history_pages={1: history_page(items=[history_record()])},
    )
    read_adapter = BuffReadOnlyAdapter(
        client,
        account_id=IDENTITY["account_id"],
    )
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: read_adapter},
        timeout_seconds=5.0,
        clock=lambda: 15.0,
    )

    result = coordinator.step(before)

    assert result.persisted is False
    assert result.after == before
    assert result.decision.detail == "read_result_unknown"
    assert client.steam_calls == 1
    assert client.history_calls == []
    assert store.advance_calls == []


def test_repeated_unresolved_history_recovery_never_sends_or_mutates():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    pages = {
        page_num: history_page(page_num=page_num, total_page=9)
        for page_num in range(1, 4)
    }
    store = SpyStore(before)
    client = HistoryClient(history_pages=pages)
    read_adapter = BuffReadOnlyAdapter(
        client,
        account_id=IDENTITY["account_id"],
    )
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: (_ for _ in ()).throw(
            AssertionError("unresolved recovery must not read the clock")
        ),
    )

    first = coordinator.recover_result_unknown_readonly(before)
    second = coordinator.recover_result_unknown_readonly(before)

    assert first.persisted is False
    assert second.persisted is False
    assert first.after == before
    assert second.after == before
    assert first.decision.detail == "read_result_unknown"
    assert second.decision.detail == "read_result_unknown"
    assert client.steam_calls == 2
    assert client.history_calls == [
        (1, "csgo"),
        (2, "csgo"),
        (3, "csgo"),
        (1, "csgo"),
        (2, "csgo"),
        (3, "csgo"),
    ]
    assert store.advance_calls == []
    assert send_adapter.calls == []


def test_missing_offer_evidence_keeps_result_unknown_without_resend_or_write():
    before = make_delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    store = SpyStore(before)
    read_adapter = unknown_offer_adapter()
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock must not be read")),
    )

    result = coordinator.step(before)

    assert result.persisted is False
    assert result.after == before
    assert result.decision.result is AutoOfferResult.WAITING
    assert result.decision.detail == "read_result_unknown"
    assert store.advance_calls == []
    assert send_adapter.calls == []


def test_clock_failure_after_positive_read_persists_nothing_and_never_sends():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    store = SpyStore(before)
    read_adapter = success_offer_adapter()
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")),
    )

    with pytest.raises(ReadOnlyCoordinatorError, match="clock_failed"):
        coordinator.step(before)

    assert len(read_adapter.calls) == 1
    assert store.advance_calls == []
    assert send_adapter.calls == []


def test_clock_regression_blocks_recovery_without_persistence_or_resend():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    store = SpyStore(before)
    read_adapter = success_offer_adapter()
    send_adapter = NeverSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=5.0,
        allow_writes=True,
        clock=lambda: 9.0,
    )

    result = coordinator.step(before)

    assert result.persisted is False
    assert result.after == before
    assert result.decision.detail == "recovery_observation_time_invalid"
    assert store.advance_calls == []
    assert send_adapter.calls == []


def test_recovered_offer_uses_existing_exact_steam_trade_offer_path_next():
    before = make_delivery(DeliveryStatus.OFFER_ATTEMPTED)
    store = SpyStore(before)
    read_adapter = success_offer_adapter("9201")

    def steam_result(request):
        assert request.capability is PlatformCapability.READ_STEAM_TRADE_OFFER
        assert request.steam_tradeoffer_id == "9201"
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="trade_offer_read",
            evidence=SteamTradeOfferEvidence(
                steam_tradeoffer_id="9201",
                account_steam_id=IDENTITY["recipient_steam_id"],
                counterparty_steam_id="76561198000000002",
                is_our_offer=True,
                lifecycle=SteamTradeOfferLifecycle.ACTIVE,
                items_to_give=(),
                items_to_receive=(
                    TradeOfferItemEvidence(
                        appid=730,
                        contextid="2",
                        assetid="asset-source-1",
                        amount=1,
                    ),
                ),
            ),
        )

    steam_adapter = SpyAdapter(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        steam_result,
    )
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: steam_adapter,
        },
        timeout_seconds=5.0,
        clock=lambda: 12.0,
    )

    recovered = coordinator.step(before).after
    confirmed = coordinator.step(recovered)

    assert recovered.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert confirmed.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert confirmed.after.snapshot.steam_tradeoffer_id == "9201"
    assert len(read_adapter.calls) == 1
    assert len(steam_adapter.calls) == 1


def test_seller_offer_path_does_not_consume_recovery_clock():
    seller_snapshot = DeliverySnapshot(
        **IDENTITY,
        delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
        delivery_status=DeliveryStatus.AWAITING_OFFER,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
    )
    validate_delivery_snapshot(seller_snapshot)
    before = StoredDelivery(snapshot=seller_snapshot, revision=3)
    store = SpyStore(before)
    read_adapter = success_offer_adapter("9301")
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: read_adapter},
        timeout_seconds=5.0,
        clock=lambda: (_ for _ in ()).throw(AssertionError("seller path clock changed")),
    )

    result = coordinator.step(before)

    assert result.persisted is True
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_RECEIVED
    assert result.after.snapshot.steam_tradeoffer_id == "9301"
