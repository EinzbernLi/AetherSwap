from __future__ import annotations

import pytest

import app.auto_offer.host_integration as host_integration
import app.auto_offer.platform_readonly as platform_readonly
from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
)
from app.auto_offer.contracts import (
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
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
        revision=4,
    )


class _BuffReader:
    def __init__(self):
        self.realtime_calls = 0
        self.history_calls = []

    def get_steam_trades(self):
        self.realtime_calls += 1
        return []

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


class _TradeReader:
    def __init__(self):
        self.calls = []

    def __call__(self, tradeoffer_id):
        self.calls.append(tradeoffer_id)
        return {
            "steam_tradeoffer_id": tradeoffer_id,
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


def test_zero_persisted_lineage_keeps_current_exact_binding():
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset(),
    )
    assert lineage.current_account_id == CURRENT_ACCOUNT
    assert lineage.target_account_id == CURRENT_ACCOUNT
    assert lineage.accepted_account_ids == frozenset({CURRENT_ACCOUNT})


def test_one_persisted_lineage_selects_that_exact_binding():
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    assert lineage.current_account_id == CURRENT_ACCOUNT
    assert lineage.target_account_id == HISTORICAL_ACCOUNT
    assert lineage.accepted_account_ids == frozenset(
        {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
    )


def test_multiple_distinct_persisted_lineages_fail_closed():
    with pytest.raises(
        PlatformAdapterProtocolError,
        match="persisted account lineage is ambiguous",
    ):
        platform_readonly._make_recovery_account_lineage(
            CURRENT_ACCOUNT,
            frozenset({HISTORICAL_ACCOUNT, SECOND_HISTORICAL_ACCOUNT}),
        )


def test_normal_buff_adapter_remains_one_current_account_binding():
    reader = _BuffReader()
    adapter = BuffReadOnlyAdapter(reader, account_id=CURRENT_ACCOUNT)

    rejected = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))
    accepted = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=CURRENT_ACCOUNT)
    )

    assert rejected.status is PlatformResultStatus.FAILURE
    assert rejected.detail == "identity_mismatch"
    assert accepted.status is PlatformResultStatus.RESULT_UNKNOWN
    assert reader.realtime_calls == 1


def test_recovery_buff_adapter_binds_only_one_historical_account():
    reader = _BuffReader()
    lineage = platform_readonly._make_recovery_account_lineage(
        CURRENT_ACCOUNT,
        frozenset({HISTORICAL_ACCOUNT}),
    )
    adapter = BuffReadOnlyAdapter(
        reader,
        account_id=CURRENT_ACCOUNT,
        recovery_lineage=lineage,
    )

    accepted = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))
    calls_after_accepted = reader.realtime_calls
    rejected_current = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=CURRENT_ACCOUNT)
    )
    rejected_other = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=UNRELATED_ACCOUNT)
    )

    assert accepted.status is PlatformResultStatus.RESULT_UNKNOWN
    assert accepted.request.account_id == HISTORICAL_ACCOUNT
    assert rejected_current.status is PlatformResultStatus.FAILURE
    assert rejected_other.status is PlatformResultStatus.FAILURE
    assert reader.realtime_calls == calls_after_accepted == 1


def test_recovery_steam_adapter_keeps_historical_account_and_steam_exact():
    reader = _TradeReader()
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

    accepted = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            tradeoffer_id="offer-1",
        )
    )
    rejected_account = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            account_id=CURRENT_ACCOUNT,
            tradeoffer_id="offer-1",
        )
    )
    rejected_recipient = adapter.execute(
        _request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            recipient=OTHER_STEAM_ID,
            tradeoffer_id="offer-1",
        )
    )

    assert accepted.status is PlatformResultStatus.SUCCESS
    assert accepted.request.account_id == HISTORICAL_ACCOUNT
    assert accepted.request.recipient_steam_id == STEAM_ID
    assert rejected_account.status is PlatformResultStatus.FAILURE
    assert rejected_recipient.status is PlatformResultStatus.FAILURE
    assert reader.calls == ["offer-1"]


def _patch_current_identity(monkeypatch, *, credential_steam_id=STEAM_ID):
    monkeypatch.setattr(host_integration, "get_current_id", lambda: CURRENT_ACCOUNT)
    monkeypatch.setattr(
        host_integration,
        "get_account",
        lambda requested: {"id": requested, "steam_id": STEAM_ID},
    )
    monkeypatch.setattr(
        host_integration,
        "get_steam_credentials",
        lambda: {"steam_id": credential_steam_id, "cookies": "fake-cookie"},
    )


def _patch_builder_runtime(monkeypatch, recoverable, captured):
    class FakeSession:
        verify = True

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeStore:
        def __init__(self, _path):
            self.closed = False

        def initialize_existing(self):
            pass

        def list_recoverable(self):
            return list(recoverable)

        def get_by_purchase_id(self, purchase_id):
            return next(
                (
                    item
                    for item in recoverable
                    if item.snapshot.purchase_id == purchase_id
                ),
                None,
            )

        def close(self):
            self.closed = True

    class FakeTradeHttpReader:
        def __init__(self, *_args, **_kwargs):
            self.bound_account_steam_id = STEAM_ID

        def __call__(self, *_args):
            raise AssertionError("builder performed Steam I/O")

    class FakeCompletedHttpReader(FakeTradeHttpReader):
        pass

    class FakeCoordinator:
        def __init__(self, _store, adapters, **kwargs):
            captured["adapters"] = adapters
            captured["kwargs"] = kwargs

    monkeypatch.setattr(host_integration, "SteamHostEgressSession", FakeSession)
    monkeypatch.setattr(host_integration, "AutoOfferStore", FakeStore)
    monkeypatch.setattr(host_integration, "SteamTradeOfferHttpReader", FakeTradeHttpReader)
    monkeypatch.setattr(
        host_integration,
        "SteamCompletedTradeHttpReader",
        FakeCompletedHttpReader,
    )
    monkeypatch.setattr(host_integration, "DeliveryCoordinator", FakeCoordinator)


def test_public_builder_empty_store_keeps_current_binding_and_zero_writes(monkeypatch):
    _patch_current_identity(monkeypatch)
    captured = {}
    _patch_builder_runtime(monkeypatch, [], captured)
    buff = _BuffReader()

    maintenance = host_integration.build_host_recovery_only_maintenance(
        buff_client=buff,
        store_path="unused.db",
    )

    assert maintenance._bridge.accepted_account_ids == frozenset({CURRENT_ACCOUNT})
    assert captured["kwargs"]["allow_writes"] is False
    assert captured["kwargs"]["allow_confirmation_writes"] is False
    assert captured["kwargs"]["allow_accept_writes"] is False
    adapter = captured["adapters"][PlatformCapability.READ_OFFER_STATE]
    current = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=CURRENT_ACCOUNT)
    )
    historical = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))
    assert current.status is PlatformResultStatus.RESULT_UNKNOWN
    assert historical.status is PlatformResultStatus.FAILURE
    assert buff.realtime_calls == 1
    maintenance.close()


def test_public_builder_one_same_steam_row_binds_persisted_lineage_only(monkeypatch):
    _patch_current_identity(monkeypatch)
    captured = {}
    same_steam = _stored(account_id=HISTORICAL_ACCOUNT)
    other_recipient = _stored(
        "order-2",
        account_id=UNRELATED_ACCOUNT,
        recipient=OTHER_STEAM_ID,
    )
    _patch_builder_runtime(monkeypatch, [same_steam, other_recipient], captured)
    buff = _BuffReader()

    maintenance = host_integration.build_host_recovery_only_maintenance(
        buff_client=buff,
        store_path="unused.db",
    )

    assert maintenance._bridge.accepted_account_ids == frozenset(
        {CURRENT_ACCOUNT, HISTORICAL_ACCOUNT}
    )
    adapter = captured["adapters"][PlatformCapability.READ_OFFER_STATE]
    historical = adapter.execute(_request(PlatformCapability.READ_OFFER_STATE))
    current = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=CURRENT_ACCOUNT)
    )
    unrelated = adapter.execute(
        _request(PlatformCapability.READ_OFFER_STATE, account_id=UNRELATED_ACCOUNT)
    )
    assert historical.status is PlatformResultStatus.RESULT_UNKNOWN
    assert current.status is PlatformResultStatus.FAILURE
    assert unrelated.status is PlatformResultStatus.FAILURE
    assert buff.realtime_calls == 1
    maintenance.close()


def test_public_builder_multiple_same_steam_lineages_fail_before_reader_dispatch(monkeypatch):
    _patch_current_identity(monkeypatch)
    captured = {}
    first = _stored("order-1", account_id=HISTORICAL_ACCOUNT)
    second = _stored("order-2", account_id=SECOND_HISTORICAL_ACCOUNT)
    _patch_builder_runtime(monkeypatch, [first, second], captured)
    buff = _BuffReader()

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="recovery_only_bridge_build_failed",
    ):
        host_integration.build_host_recovery_only_maintenance(
            buff_client=buff,
            store_path="unused.db",
        )

    assert buff.realtime_calls == 0
    assert captured == {}


def test_public_builder_credential_steam_mismatch_fails_before_session(monkeypatch):
    _patch_current_identity(monkeypatch, credential_steam_id=OTHER_STEAM_ID)

    class MustNotBuildSession:
        def __init__(self):
            raise AssertionError("session built before Steam identity proof")

    monkeypatch.setattr(host_integration, "SteamHostEgressSession", MustNotBuildSession)

    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="steam_identity_mismatch",
    ):
        host_integration.build_host_recovery_only_maintenance(
            buff_client=_BuffReader(),
            store_path="unused.db",
        )
