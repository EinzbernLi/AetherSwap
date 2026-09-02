from __future__ import annotations

import pytest

import app.auto_offer.host_integration as host_integration
from app.auto_offer.adapters import (
    OfferStateEvidence,
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
from app.auto_offer.coordinator import DeliveryCoordinator
from app.auto_offer.platform_readonly import (
    BuffReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
    _make_recovery_account_lineage,
)
from app.auto_offer.store import StoredDelivery


CURRENT_ACCOUNT = "registry-current"
HISTORICAL_ACCOUNT = "deployment-local-old"
UNRELATED_ACCOUNT = "unrelated-local"
STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"


def _request(
    capability: PlatformCapability,
    *,
    account_id: str = HISTORICAL_ACCOUNT,
    tradeoffer_id: str | None = None,
) -> PlatformRequest:
    return PlatformRequest(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id=account_id,
        recipient_steam_id=STEAM_ID,
        revision=4,
        capability=capability,
        timeout_seconds=5.0,
        steam_tradeoffer_id=tradeoffer_id,
    )


def _stored(
    order_id: str = "order-1",
    *,
    account_id: str = HISTORICAL_ACCOUNT,
    recipient: str = STEAM_ID,
    revision: int = 4,
) -> StoredDelivery:
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id=f"buff:{order_id}",
            buff_order_id=order_id,
            account_id=account_id,
            recipient_steam_id=recipient,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            steam_tradeoffer_id=None,
            offer_attempted_at=10.0,
            offer_sent_at=None,
            received_at=None,
            delivery_error="write_result_unknown",
            pending_receipt=True,
            assetid=None,
        ),
        revision=revision,
    )


def _host_row(order_id: str = "order-1", *, account_id: str | None = None) -> dict:
    row = {
        "_db_id": 1,
        "buff_order_id": order_id,
        "pending_receipt": True,
        "assetid": None,
    }
    if account_id is not None:
        row["account_id"] = account_id
    return row


class _BuffReader:
    def __init__(self, history_pages=None):
        self.history_pages = history_pages or {}
        self.realtime_calls = 0
        self.history_calls = []

    def get_steam_trades(self):
        self.realtime_calls += 1
        return []

    def get_buy_order_history_page(self, page_num, game="csgo"):
        self.history_calls.append((page_num, game))
        return self.history_pages.get(page_num)


class _TradeReader:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, tradeoffer_id):
        self.calls.append(tradeoffer_id)
        return self.payload


def test_recovery_lineage_keeps_persisted_account_id_for_realtime_and_history_reads():
    lineage = _make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    client = _BuffReader(
        {
            1: {
                "code": "OK",
                "data": {
                    "page_num": 1,
                    "page_size": 10,
                    "total_page": 1,
                    "items": [],
                },
            }
        }
    )
    adapter = BuffReadOnlyAdapter(
        client,
        account_id=CURRENT_ACCOUNT,
        recovery_lineage=lineage,
    )

    realtime = _request(PlatformCapability.READ_OFFER_STATE)
    realtime_result = adapter.execute(realtime)
    historical = _request(PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    historical_result = adapter.execute(historical)

    assert realtime_result.request is realtime
    assert realtime_result.request.account_id == HISTORICAL_ACCOUNT
    assert historical_result.request is historical
    assert historical_result.request.account_id == HISTORICAL_ACCOUNT
    assert realtime_result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert historical_result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert client.realtime_calls == 1
    assert client.history_calls == [(1, "csgo")]


def test_recovery_lineage_exact_steam_trade_preserves_persisted_account_and_recipient():
    lineage = _make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    reader = _TradeReader(
        {
            "steam_tradeoffer_id": "offer-1",
            "account_steam_id": STEAM_ID,
            "counterparty_steam_id": OTHER_STEAM_ID,
            "is_our_offer": True,
            "lifecycle": "active",
            "items_to_give": [],
            "items_to_receive": [
                {
                    "appid": 730,
                    "contextid": "2",
                    "assetid": "asset-1",
                    "amount": 1,
                }
            ],
        }
    )
    adapter = SteamTradeOfferReadOnlyAdapter(
        reader,
        account_id=CURRENT_ACCOUNT,
        recipient_steam_id=STEAM_ID,
        recovery_lineage=lineage,
    )
    request = _request(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        tradeoffer_id="offer-1",
    )

    result = adapter.execute(request)

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.request is request
    assert result.request.account_id == HISTORICAL_ACCOUNT
    assert result.request.recipient_steam_id == STEAM_ID
    assert reader.calls == ["offer-1"]


def test_unapproved_recovery_account_fails_before_buff_reader():
    lineage = _make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    client = _BuffReader()
    adapter = BuffReadOnlyAdapter(
        client,
        account_id=CURRENT_ACCOUNT,
        recovery_lineage=lineage,
    )

    result = adapter.execute(
        _request(
            PlatformCapability.READ_OFFER_STATE,
            account_id=UNRELATED_ACCOUNT,
        )
    )

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert client.realtime_calls == 0


class _CoordinatorAdapter:
    capabilities = frozenset({PlatformCapability.READ_OFFER_STATE})

    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.RESULT_UNKNOWN,
            detail="order_not_proven",
        )


class _CoordinatorBridge:
    capabilities = host_integration._RECOVERY_ONLY_CAPABILITIES

    def __init__(self, stored):
        self.account_id = CURRENT_ACCOUNT
        self.recipient_steam_id = STEAM_ID
        self.accepted_account_ids = frozenset(
            {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
        )
        self.current = stored
        self.adapter = _CoordinatorAdapter()
        self.coordinator = DeliveryCoordinator(
            self,
            {PlatformCapability.READ_OFFER_STATE: self.adapter},
            timeout_seconds=5.0,
            allow_writes=False,
            allow_confirmation_writes=False,
            allow_accept_writes=False,
        )
        self.advance_calls = []

    def list_recoverable(self):
        return (self.current,)

    def get_by_purchase_id(self, purchase_id):
        return self.current if self.current.snapshot.purchase_id == purchase_id else None

    def advance(self, current, target):
        self.advance_calls.append((current, target))
        raise AssertionError("unknown read must not mutate Store")

    def recover_result_unknown_readonly(self, delivery):
        return self.coordinator.recover_result_unknown_readonly(delivery)

    def step(self, delivery):
        return self.coordinator.step(delivery)

    def close(self):
        pass


def test_host_recovery_admits_historical_store_lineage_and_keeps_request_identity():
    stored = _stored()
    bridge = _CoordinatorBridge(stored)
    maintenance = host_integration.HostRecoveryOnlyMaintenance(bridge)

    outcome = maintenance.run_recovery_tick([_host_row()])

    assert outcome.result is AutoOfferResult.RESULT_UNKNOWN
    assert len(bridge.adapter.calls) == 1
    assert bridge.adapter.calls[0].account_id == HISTORICAL_ACCOUNT
    assert bridge.adapter.calls[0].recipient_steam_id == STEAM_ID
    assert bridge.advance_calls == []


def test_host_optional_account_id_must_match_persisted_store_lineage():
    stored = _stored()
    bridge = _CoordinatorBridge(stored)
    maintenance = host_integration.HostRecoveryOnlyMaintenance(bridge)

    outcome = maintenance.run_recovery_tick(
        [_host_row(account_id=CURRENT_ACCOUNT)]
    )

    assert outcome.result is AutoOfferResult.BLOCKED
    assert bridge.adapter.calls == []
    assert bridge.advance_calls == []


def test_host_unrelated_recipient_row_is_not_admitted_by_account_alias():
    target = _stored()
    unrelated = _stored(
        "order-2",
        account_id=HISTORICAL_ACCOUNT,
        recipient=OTHER_STEAM_ID,
    )
    bridge = _CoordinatorBridge(target)
    bridge.list_recoverable = lambda: (target, unrelated)
    maintenance = host_integration.HostRecoveryOnlyMaintenance(bridge)

    outcome = maintenance.run_recovery_tick([_host_row()])

    assert outcome.result is AutoOfferResult.BLOCKED
    assert bridge.adapter.calls == []
    assert bridge.advance_calls == []


def test_public_recovery_builder_derives_lineage_only_from_same_steam_rows(
    monkeypatch,
):
    same_steam = _stored(account_id=HISTORICAL_ACCOUNT)
    unrelated = _stored(
        "order-2",
        account_id=UNRELATED_ACCOUNT,
        recipient=OTHER_STEAM_ID,
    )
    captured = {}

    class FakeSession:
        verify = True

        def close(self):
            pass

    class FakeStore:
        def __init__(self, _path):
            self.closed = False

        def initialize_existing(self):
            pass

        def list_recoverable(self):
            return [same_steam, unrelated]

        def close(self):
            self.closed = True

    class FakeReader:
        def __init__(self, *_args, **_kwargs):
            self.bound_account_steam_id = STEAM_ID

    class FakeBuff:
        def get_steam_trades(self):
            raise AssertionError("builder performed BUFF I/O")

    class FakeReadAdapter:
        def __init__(self, *_args, **kwargs):
            self.kwargs = kwargs

    class FakeCoordinator:
        def __init__(self, _store, adapters, **kwargs):
            captured["adapters"] = adapters
            captured["kwargs"] = kwargs

    monkeypatch.setattr(host_integration, "get_current_id", lambda: CURRENT_ACCOUNT)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": STEAM_ID, "cookies": "fake-cookie"},
    )
    monkeypatch.setattr(host_integration, "SteamHostEgressSession", FakeSession)
    monkeypatch.setattr(host_integration, "AutoOfferStore", FakeStore)
    monkeypatch.setattr(host_integration, "SteamTradeOfferHttpReader", FakeReader)
    monkeypatch.setattr(host_integration, "SteamCompletedTradeHttpReader", FakeReader)
    monkeypatch.setattr(host_integration, "BuffReadOnlyAdapter", FakeReadAdapter)
    monkeypatch.setattr(
        host_integration,
        "SteamTradeOfferReadOnlyAdapter",
        FakeReadAdapter,
    )
    monkeypatch.setattr(
        host_integration,
        "SteamCompletedTradeReadOnlyAdapter",
        FakeReadAdapter,
    )
    monkeypatch.setattr(host_integration, "DeliveryCoordinator", FakeCoordinator)

    maintenance = host_integration.build_host_recovery_only_maintenance(
        buff_client=FakeBuff(),
        store_path="unused.db",
    )

    assert maintenance._bridge.accepted_account_ids == frozenset(
        {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
    )
    assert UNRELATED_ACCOUNT not in maintenance._bridge.accepted_account_ids
    assert captured["kwargs"]["allow_writes"] is False
    assert set(captured["adapters"]) == host_integration._RECOVERY_ONLY_CAPABILITIES
    assert (
        captured["adapters"][PlatformCapability.READ_OFFER_STATE].kwargs[
            "recovery_lineage"
        ].accepted_account_ids
        == frozenset({CURRENT_ACCOUNT, HISTORICAL_ACCOUNT})
    )
    maintenance.close()
