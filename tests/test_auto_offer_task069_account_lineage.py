from __future__ import annotations

import pytest

import app.auto_offer.host_integration as host_integration
import app.auto_offer.platform_readonly as platform_readonly
from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
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
)
from app.auto_offer.store import StoredDelivery


CURRENT_ACCOUNT = "registry-current"
HISTORICAL_ACCOUNT = "deployment-local-old"
SECOND_HISTORICAL_ACCOUNT = "deployment-local-second"
UNRELATED_ACCOUNT = "unrelated-local"
STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"


def _request(
    capability: PlatformCapability,
    *,
    account_id: str = HISTORICAL_ACCOUNT,
    recipient: str = STEAM_ID,
    tradeoffer_id: str | None = None,
) -> PlatformRequest:
    return PlatformRequest(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id=account_id,
        recipient_steam_id=recipient,
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


def _trade_payload():
    return {
        "steam_tradeoffer_id": "offer-1",
        "account_steam_id": STEAM_ID,
        "counterparty_steam_id": OTHER_STEAM_ID,
        "is_our_offer": True,
        "lifecycle": "active",
        "items_to_give": [],
        "items_to_receive": [
            {"appid": 730, "contextid": "2", "assetid": "asset-1", "amount": 1}
        ],
    }


def test_recovery_lineage_selects_one_exact_persisted_account():
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    assert lineage.current_account_id == CURRENT_ACCOUNT
    assert lineage.target_account_id == HISTORICAL_ACCOUNT
    assert lineage.accepted_account_ids == frozenset(
        {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
    )


@pytest.mark.parametrize(
    "persisted",
    [
        frozenset(),
        frozenset({HISTORICAL_ACCOUNT, SECOND_HISTORICAL_ACCOUNT}),
    ],
)
def test_recovery_lineage_ambiguity_fails_closed(persisted):
    with pytest.raises(
        PlatformAdapterProtocolError,
        match="persisted account lineage must be unique",
    ):
        platform_readonly._make_recovery_account_lineage(CURRENT_ACCOUNT, persisted)


def test_non_recovery_buff_adapter_remains_single_current_account_binding():
    client = _BuffReader()
    adapter = BuffReadOnlyAdapter(client, account_id=CURRENT_ACCOUNT)

    result = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert client.realtime_calls == 0


def test_recovery_buff_adapter_binds_only_the_one_historical_account():
    client = _BuffReader()
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    adapter = BuffReadOnlyAdapter(
        client,
        account_id=CURRENT_ACCOUNT,
        recovery_lineage=lineage,
    )

    historical = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))
    assert historical.status is PlatformResultStatus.RESULT_UNKNOWN
    assert historical.request.account_id == HISTORICAL_ACCOUNT
    assert client.realtime_calls == 1

    calls_before = client.realtime_calls
    for rejected_account in (CURRENT_ACCOUNT, UNRELATED_ACCOUNT):
        rejected = adapter.execute(
            _request(
                PlatformCapability.READ_OFFER_STATE,
                account_id=rejected_account,
            )
        )
        assert rejected.status is PlatformResultStatus.FAILURE
        assert rejected.detail == "identity_mismatch"
    assert client.realtime_calls == calls_before


def test_recovery_steam_adapter_keeps_exact_historical_and_steam_binding():
    reader = _TradeReader(_trade_payload())
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    adapter = SteamTradeOfferReadOnlyAdapter(
        reader,
        account_id=CURRENT_ACCOUNT,
        recipient_steam_id=STEAM_ID,
        recovery_lineage=lineage,
    )

    success = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            tradeoffer_id="offer-1",
        )
    )
    assert success.status is PlatformResultStatus.SUCCESS
    assert success.request.account_id == HISTORICAL_ACCOUNT
    assert success.request.recipient_steam_id == STEAM_ID
    assert reader.calls == ["offer-1"]

    wrong_account = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            account_id=CURRENT_ACCOUNT,
            tradeoffer_id="offer-1",
        )
    )
    wrong_recipient = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            recipient=OTHER_STEAM_ID,
            tradeoffer_id="offer-1",
        )
    )
    assert wrong_account.status is PlatformResultStatus.FAILURE
    assert wrong_recipient.status is PlatformResultStatus.FAILURE
    assert reader.calls == ["offer-1"]


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

    def __init__(self, stored, *, accepted_account_ids=None):
        self.account_id = CURRENT_ACCOUNT
        self.recipient_steam_id = STEAM_ID
        self.accepted_account_ids = accepted_account_ids or frozenset(
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


def test_host_optional_account_id_must_match_exact_persisted_store_lineage():
    stored = _stored()
    bridge = _CoordinatorBridge(stored)
    maintenance = host_integration.HostRecoveryOnlyMaintenance(bridge)

    outcome = maintenance.run_recovery_tick([_host_row(account_id=CURRENT_ACCOUNT)])

    assert outcome.result is AutoOfferResult.BLOCKED
    assert bridge.adapter.calls == []
    assert bridge.advance_calls == []


def test_host_same_steam_second_lineage_cannot_be_borrowed_for_target_row():
    stored = _stored()
    bridge = _CoordinatorBridge(
        stored,
        accepted_account_ids=frozenset(
            {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT, SECOND_HISTORICAL_ACCOUNT}
        ),
    )
    maintenance = host_integration.HostRecoveryOnlyMaintenance(bridge)

    outcome = maintenance.run_recovery_tick(
        [_host_row(account_id=SECOND_HISTORICAL_ACCOUNT)]
    )

    assert outcome.result is AutoOfferResult.BLOCKED
    assert bridge.adapter.calls == []


def test_public_recovery_builder_uses_one_store_lineage_and_keeps_writes_disabled(
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
            pass
        def initialize_existing(self):
            pass
        def list_recoverable(self):
            return [same_steam, unrelated]
        def close(self):
            pass

    class FakeTradeHttpReader:
        instance = None
        def __init__(self, *_args, **_kwargs):
            self.bound_account_steam_id = STEAM_ID
            self.calls = []
            type(self).instance = self
        def __call__(self, tradeoffer_id):
            self.calls.append(tradeoffer_id)
            return _trade_payload()

    class FakeCompletedHttpReader:
        def __init__(self, *_args, **_kwargs):
            self.bound_account_steam_id = STEAM_ID
        def __call__(self, *_args):
            raise AssertionError("completed reader should not be called")

    class FakeBuff(_BuffReader):
        def get_buy_order_history_page(self, page_num, game="csgo"):
            self.history_calls.append((page_num, game))
            return {
                "code": "OK",
                "data": {
                    "page_num": page_num,
                    "page_size": 10,
                    "total_page": 1,
                    "items": [],
                },
            }

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
    monkeypatch.setattr(host_integration, "SteamTradeOfferHttpReader", FakeTradeHttpReader)
    monkeypatch.setattr(
        host_integration, "SteamCompletedTradeHttpReader", FakeCompletedHttpReader
    )
    monkeypatch.setattr(host_integration, "DeliveryCoordinator", FakeCoordinator)

    buff = FakeBuff()
    maintenance = host_integration.build_host_recovery_only_maintenance(
        buff_client=buff,
        store_path="unused.db",
    )

    assert maintenance._bridge.accepted_account_ids == frozenset(
        {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
    )
    assert captured["kwargs"]["allow_writes"] is False
    assert captured["kwargs"]["allow_confirmation_writes"] is False
    assert captured["kwargs"]["allow_accept_writes"] is False

    realtime = captured["adapters"][PlatformCapability.READ_OFFER_STATE].execute(
        _request(PlatformCapability.READ_OFFER_STATE)
    )
    assert realtime.status is PlatformResultStatus.RESULT_UNKNOWN
    assert realtime.request.account_id == HISTORICAL_ACCOUNT
    assert buff.realtime_calls == 1

    calls_before = buff.realtime_calls
    rejected = captured["adapters"][PlatformCapability.READ_OFFER_STATE].execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=CURRENT_ACCOUNT)
    )
    assert rejected.status is PlatformResultStatus.FAILURE
    assert buff.realtime_calls == calls_before

    maintenance.close()


def test_public_recovery_builder_credential_steam_mismatch_fails_before_session(
    monkeypatch,
):
    monkeypatch.setattr(host_integration, "get_current_id", lambda: CURRENT_ACCOUNT)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": OTHER_STEAM_ID, "cookies": "fake-cookie"},
    )

    class MustNotBuildSession:
        def __init__(self):
            raise AssertionError("session built before Steam identity proof")

    monkeypatch.setattr(host_integration, "SteamHostEgressSession", MustNotBuildSession)

    with pytest.raises(host_integration.HostAutoOfferIntegrationError, match="steam_identity_mismatch"):
        host_integration.build_host_recovery_only_maintenance(
            buff_client=_BuffReader(),
            store_path="unused.db",
        )
