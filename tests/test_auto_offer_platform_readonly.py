import importlib
import math
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from app.auto_offer.adapters import (
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformAdapter,
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)
from app.auto_offer.platform_readonly import (
    BUFF_CAPABILITIES,
    STEAM_INVENTORY_CAPABILITIES,
    STEAM_TRADE_OFFER_CAPABILITIES,
    BuffReadOnlyAdapter,
    SteamInventoryReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)


def request(**changes):
    value = PlatformRequest(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
        revision=3,
        capability=PlatformCapability.READ_OFFER_STATE,
        timeout_seconds=5.0,
    )
    return replace(value, **changes)


class BuffStub:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


class InventoryStub:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, steam_id):
        self.calls.append(steam_id)
        if self.error:
            raise self.error
        return self.payload


class TradeOfferReaderStub:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, steam_tradeoffer_id):
        self.calls.append(steam_tradeoffer_id)
        if self.error:
            raise self.error
        return self.payload


def buff_record(**changes):
    value = {
        "buff_order_id": "buff-order-1",
        "seller_steam_id": "seller-1",
        "buyer_steam_id": "steam-1",
        "state": 1,
        "tradeofferid": "offer-1",
    }
    value.update(changes)
    return value


def buff_adapter(client):
    return BuffReadOnlyAdapter(client, account_id="account-1")


def steam_trade_offer_adapter(reader):
    return SteamTradeOfferReadOnlyAdapter(
        reader,
        account_id="account-1",
        recipient_steam_id="steam-1",
    )


def steam_trade_offer_request(**changes):
    value = request(
        capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
        steam_tradeoffer_id="offer-1",
    )
    return replace(value, **changes)


def steam_trade_offer_payload(**changes):
    value = {
        "steam_tradeoffer_id": "offer-1",
        "account_steam_id": "steam-1",
        "counterparty_steam_id": "steam-2",
        "is_our_offer": False,
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
    value.update(changes)
    return value


def test_import_and_constructors_have_no_io_or_thread_side_effects(monkeypatch, tmp_path):
    before = tuple(tmp_path.iterdir())
    active = threading.active_count()
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("app.auto_offer.platform_readonly")
    buff_adapter(BuffStub())
    SteamInventoryReadOnlyAdapter(
        InventoryStub(), account_id="account-1", recipient_steam_id="steam-1"
    )
    assert tuple(tmp_path.iterdir()) == before
    assert threading.active_count() == active
    assert module.BuffReadOnlyAdapter is BuffReadOnlyAdapter


def test_both_adapters_satisfy_platform_adapter_and_capabilities_are_immutable():
    buff = buff_adapter(BuffStub())
    steam = SteamInventoryReadOnlyAdapter(
        InventoryStub(), account_id="account-1", recipient_steam_id="steam-1"
    )
    assert isinstance(buff, PlatformAdapter)
    assert isinstance(steam, PlatformAdapter)
    assert buff.capabilities == BUFF_CAPABILITIES
    assert steam.capabilities == STEAM_INVENTORY_CAPABILITIES
    with pytest.raises(AttributeError):
        buff.capabilities.add(PlatformCapability.SEND_OFFER)


def test_send_offer_and_unknown_capability_are_unsupported_without_buff_read():
    client = BuffStub([buff_record()])
    adapter = buff_adapter(client)
    for capability in (PlatformCapability.SEND_OFFER, PlatformCapability.READ_INVENTORY_STATE):
        result = adapter.execute(request(capability=capability))
        assert result.status is PlatformResultStatus.UNSUPPORTED
        assert result.detail == "unsupported_capability"
    assert client.calls == 0


def test_buff_preserves_exact_request_identity_and_revision():
    item = request(capability=PlatformCapability.READ_DELIVERY_DIRECTION)
    result = buff_adapter(BuffStub([buff_record()])).execute(item)
    assert result.request is item
    assert result.request.purchase_id == "purchase-1"
    assert result.request.buff_order_id == "buff-order-1"
    assert result.request.account_id == "account-1"
    assert result.request.recipient_steam_id == "steam-1"
    assert result.request.revision == 3


@pytest.mark.parametrize(
    "payload",
    [[], [buff_record(buff_order_id="other")], [{"id": "buff-order-1"}]],
)
def test_buff_without_one_exact_canonical_order_is_unknown(payload):
    result = buff_adapter(BuffStub(payload)).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"


def test_buff_generic_id_goods_id_and_name_cannot_prove_order():
    payload = [
        {
            "id": "buff-order-1",
            "goods_id": "buff-order-1",
            "name": "buff-order-1",
            "market_hash_name": "buff-order-1",
        }
    ]
    result = buff_adapter(BuffStub(payload)).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN


def test_buff_multiple_exact_matches_are_malformed():
    result = buff_adapter(
        BuffStub([buff_record(), buff_record(tradeofferid="offer-2")])
    ).execute(request())
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "ambiguous_order"


def test_buff_unique_direction_requires_recipient_and_proves_seller_send():
    result = buff_adapter(
        BuffStub([buff_record()])
    ).execute(request(capability=PlatformCapability.READ_DELIVERY_DIRECTION))
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "seller_sends_offer"
    assert result.evidence == DeliveryDirectionEvidence()

    mismatch = buff_adapter(
        BuffStub([buff_record(buyer_steam_id="other-steam")])
    ).execute(request(capability=PlatformCapability.READ_DELIVERY_DIRECTION))
    assert mismatch.status is PlatformResultStatus.FAILURE
    assert mismatch.detail == "identity_mismatch"
    assert mismatch.is_success is False


def test_buff_offer_state_requires_exact_order_trade_offer_and_known_pending_state():
    result = buff_adapter(BuffStub([buff_record()])).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_pending"
    assert result.evidence == OfferStateEvidence("offer-1")

    for record in (
        buff_record(tradeofferid=None),
        buff_record(state="unknown"),
    ):
        result = buff_adapter(BuffStub([record])).execute(request())
        assert result.status is PlatformResultStatus.RESULT_UNKNOWN
        assert result.is_success is False


@pytest.mark.parametrize("payload", [None, {"code": "OK", "data": []}, ["not-a-record"]])
def test_buff_unavailable_or_malformed_payload_fails_closed(payload):
    result = buff_adapter(BuffStub(payload)).execute(request())
    assert result.is_success is False
    assert result.status in {
        PlatformResultStatus.RESULT_UNKNOWN,
        PlatformResultStatus.MALFORMED,
    }


def test_buff_timeout_and_exception_are_not_success():
    timeout = buff_adapter(BuffStub(error=TimeoutError())).execute(request())
    failure = buff_adapter(BuffStub(error=RuntimeError("secret"))).execute(request())
    assert timeout.status is PlatformResultStatus.TIMEOUT
    assert timeout.detail == "timeout"
    assert failure.status is PlatformResultStatus.FAILURE
    assert failure.detail == "network_failure"
    assert "secret" not in str(failure.detail)


def test_steam_identity_mismatch_does_not_call_reader():
    reader = InventoryStub({"success": 1, "assets": []})
    adapter = SteamInventoryReadOnlyAdapter(
        reader, account_id="account-1", recipient_steam_id="steam-1"
    )
    for changes in (
        {"account_id": "other-account", "capability": PlatformCapability.READ_INVENTORY_STATE},
        {"recipient_steam_id": "other-steam", "capability": PlatformCapability.READ_INVENTORY_STATE},
    ):
        result = adapter.execute(request(**changes))
        assert result.status is PlatformResultStatus.FAILURE
        assert result.detail == "identity_mismatch"
    assert reader.calls == []


def test_buff_account_mismatch_does_not_call_client():
    client = BuffStub([buff_record()])
    result = buff_adapter(client).execute(request(account_id="other-account"))
    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert client.calls == 0


def test_steam_unsupported_capability_does_not_call_reader():
    reader = InventoryStub({"success": 1, "assets": []})
    adapter = SteamInventoryReadOnlyAdapter(
        reader, account_id="account-1", recipient_steam_id="steam-1"
    )
    result = adapter.execute(request(capability=PlatformCapability.SEND_OFFER))
    assert result.status is PlatformResultStatus.UNSUPPORTED
    assert reader.calls == []


def test_buff_offer_state_requires_exact_recipient_identity():
    record_without_recipient = buff_record()
    del record_without_recipient["buyer_steam_id"]
    wrong_recipient = buff_adapter(
        BuffStub([buff_record(buyer_steam_id="other-steam")])
    ).execute(request())
    missing_recipient = buff_adapter(
        BuffStub([record_without_recipient])
    ).execute(request())
    assert wrong_recipient.status is PlatformResultStatus.FAILURE
    assert wrong_recipient.detail == "identity_mismatch"
    assert wrong_recipient.is_success is False
    assert missing_recipient.status is PlatformResultStatus.RESULT_UNKNOWN
    assert missing_recipient.detail == "order_not_proven"
    assert missing_recipient.is_success is False


def test_injected_platform_result_is_untrusted_raw_payload():
    item = request()
    buff = buff_adapter(
        BuffStub(
            PlatformResult(
                item,
                PlatformResultStatus.SUCCESS,
                "forged",
                OfferStateEvidence("offer-1"),
            )
        )
    ).execute(item)
    inventory_request = request(capability=PlatformCapability.READ_INVENTORY_STATE)
    steam = SteamInventoryReadOnlyAdapter(
        InventoryStub(
            PlatformResult(
                inventory_request,
                PlatformResultStatus.SUCCESS,
                "forged",
                InventoryStateEvidence(("asset-1",)),
            )
        ),
        account_id="account-1",
        recipient_steam_id="steam-1",
    ).execute(inventory_request)
    assert buff.status is PlatformResultStatus.MALFORMED
    assert buff.detail == "malformed_payload"
    assert steam.status is PlatformResultStatus.MALFORMED
    assert steam.detail == "malformed_payload"


def test_steam_none_malformed_auth_and_valid_snapshot_mapping():
    base = {"capability": PlatformCapability.READ_INVENTORY_STATE}
    none_result = SteamInventoryReadOnlyAdapter(
        InventoryStub(None), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(**base))
    malformed = SteamInventoryReadOnlyAdapter(
        InventoryStub({"assets": []}), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(**base))
    auth = SteamInventoryReadOnlyAdapter(
        InventoryStub({"auth_expired": True}), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(**base))
    valid = SteamInventoryReadOnlyAdapter(
        InventoryStub({"success": 1, "assets": [], "descriptions": []}),
        account_id="account-1", recipient_steam_id="steam-1",
    ).execute(request(**base))
    assert none_result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert malformed.status is PlatformResultStatus.MALFORMED
    assert auth.status is PlatformResultStatus.FAILURE
    assert auth.detail == "auth_failed"
    assert valid.status is PlatformResultStatus.SUCCESS
    assert valid.detail == "inventory_snapshot_readable"
    assert valid.evidence == InventoryStateEvidence((), 0)
    assert "received" not in (valid.detail or "")


def test_steam_timeout_and_generic_exception_fail_closed():
    base = {"capability": PlatformCapability.READ_INVENTORY_STATE}
    timeout = SteamInventoryReadOnlyAdapter(
        InventoryStub(error=TimeoutError()), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(**base))
    failure = SteamInventoryReadOnlyAdapter(
        InventoryStub(error=RuntimeError("credential")), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(**base))
    assert timeout.status is PlatformResultStatus.TIMEOUT
    assert failure.status is PlatformResultStatus.FAILURE
    assert failure.detail == "network_failure"


def test_constructor_rejects_non_callable_or_invalid_identity():
    with pytest.raises(PlatformAdapterProtocolError):
        BuffReadOnlyAdapter(object(), account_id="account-1")
    with pytest.raises(PlatformAdapterProtocolError):
        BuffReadOnlyAdapter(BuffStub(), account_id=" account-1")
    with pytest.raises(PlatformAdapterProtocolError):
        SteamInventoryReadOnlyAdapter(
            object(), account_id="account-1", recipient_steam_id="steam-1"
        )
    with pytest.raises(PlatformAdapterProtocolError):
        SteamInventoryReadOnlyAdapter(
            InventoryStub(), account_id=" account-1", recipient_steam_id="steam-1"
        )


def test_request_result_remain_immutable_and_adapter_does_not_mutate_request():
    item = request(capability=PlatformCapability.READ_INVENTORY_STATE)
    result = SteamInventoryReadOnlyAdapter(
        InventoryStub({"success": 1, "assets": []}),
        account_id="account-1", recipient_steam_id="steam-1",
    ).execute(item)
    with pytest.raises(FrozenInstanceError):
        item.revision = 4
    assert result.request is item
    assert item.revision == 3


def test_steam_inventory_evidence_uses_only_canonical_asset_ids():
    item = request(capability=PlatformCapability.READ_INVENTORY_STATE)
    payload = {
        "success": 1,
        "assets": [
            {"assetid": "asset-2", "name": "must-not-escape"},
            {"assetid": "asset-1", "descriptions": ["must-not-escape"]},
        ],
        "total_inventory_count": 2,
    }
    result = SteamInventoryReadOnlyAdapter(
        InventoryStub(payload), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(item)

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == InventoryStateEvidence(("asset-1", "asset-2"), 2)
    assert not hasattr(result.evidence, "name")
    assert not hasattr(result.evidence, "descriptions")
    assert "received" not in (result.detail or "")


@pytest.mark.parametrize(
    "payload",
    [
        {"success": 1, "assets": [{}]},
        {"success": 1, "assets": [{"assetid": " asset-1"}]},
        {"success": 1, "assets": [{"assetid": "asset-1"}, {"assetid": "asset-1"}]},
        {"success": 1, "assets": [{"assetid": "asset-1"}], "total_inventory_count": 0},
        {"success": 1, "total_inventory_count": 1},
    ],
)
def test_malformed_or_ambiguous_steam_assets_fail_closed(payload):
    result = SteamInventoryReadOnlyAdapter(
        InventoryStub(payload), account_id="account-1", recipient_steam_id="steam-1"
    ).execute(request(capability=PlatformCapability.READ_INVENTORY_STATE))
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_no_platform_write_or_runtime_side_effect_imports():
    module = importlib.import_module("app.auto_offer.platform_readonly")
    source = module.__file__
    text = open(source, encoding="utf-8").read()
    for forbidden in ("requests", "httpx", "aiohttp", "sleep(", "ThreadPoolExecutor", "POST", "PUT", "PATCH", "DELETE"):
        assert forbidden not in text
    assert "Store" not in text
    assert "DeliverySnapshot" not in text
    assert "executor" not in text.lower()


@pytest.mark.parametrize("value", [0, -1, True, 1.0, math.inf, math.nan])
def test_existing_request_contract_rejects_invalid_revision(value):
    with pytest.raises(PlatformAdapterProtocolError):
        request(revision=value)


def test_steam_trade_offer_adapter_declares_only_exact_read_capability():
    reader = TradeOfferReaderStub(steam_trade_offer_payload())
    adapter = steam_trade_offer_adapter(reader)

    assert isinstance(adapter, PlatformAdapter)
    assert adapter.capabilities == STEAM_TRADE_OFFER_CAPABILITIES
    assert adapter.capabilities == frozenset(
        {PlatformCapability.READ_STEAM_TRADE_OFFER}
    )
    with pytest.raises(AttributeError):
        adapter.capabilities.add(PlatformCapability.SEND_OFFER)


def test_steam_trade_offer_request_gate_does_not_call_reader():
    reader = TradeOfferReaderStub(steam_trade_offer_payload())
    adapter = steam_trade_offer_adapter(reader)

    for item in (
        request(capability=PlatformCapability.READ_OFFER_STATE),
        steam_trade_offer_request(account_id="other-account"),
        steam_trade_offer_request(recipient_steam_id="other-steam"),
    ):
        result = adapter.execute(item)
        assert result.status in {
            PlatformResultStatus.UNSUPPORTED,
            PlatformResultStatus.FAILURE,
        }
        assert result.is_success is False
    assert reader.calls == []


def test_steam_trade_offer_adapter_uses_one_exact_reader_call_and_typed_evidence():
    reader = TradeOfferReaderStub(steam_trade_offer_payload())
    result = steam_trade_offer_adapter(reader).execute(
        steam_trade_offer_request()
    )

    assert reader.calls == ["offer-1"]
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "trade_offer_active"
    assert type(result.evidence) is SteamTradeOfferEvidence
    assert result.evidence.steam_tradeoffer_id == "offer-1"
    assert result.evidence.account_steam_id == "steam-1"
    assert result.evidence.counterparty_steam_id == "steam-2"
    assert result.evidence.is_our_offer is False
    assert result.evidence.lifecycle is SteamTradeOfferLifecycle.ACTIVE
    assert result.evidence.items_to_give == ()
    assert result.evidence.items_to_receive[0].assetid == "asset-1"


def test_steam_trade_offer_adapter_preserves_multi_item_direction_and_accepted():
    payload = steam_trade_offer_payload(
        is_our_offer=True,
        lifecycle="accepted",
        items_to_give=[
            {"appid": 730, "contextid": "2", "assetid": "asset-2", "amount": 1},
            {"appid": 440, "contextid": "2", "assetid": "asset-3", "amount": 1},
        ],
        items_to_receive=[],
    )
    result = steam_trade_offer_adapter(TradeOfferReaderStub(payload)).execute(
        steam_trade_offer_request()
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "trade_offer_accepted"
    assert result.evidence.lifecycle is SteamTradeOfferLifecycle.ACCEPTED
    assert result.evidence.is_our_offer is True
    assert [item.appid for item in result.evidence.items_to_give] == [440, 730]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steam_tradeoffer_id", "other-offer"),
        ("account_steam_id", "other-steam"),
    ],
)
def test_steam_trade_offer_identity_mismatch_is_failure(field, value):
    payload = steam_trade_offer_payload(**{field: value})
    reader = TradeOfferReaderStub(payload)
    result = steam_trade_offer_adapter(reader).execute(
        steam_trade_offer_request()
    )

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert result.evidence is None
    assert reader.calls == ["offer-1"]


@pytest.mark.parametrize(
    "lifecycle",
    [
        "confirmation_need",
        "countered",
        "expired",
        "canceled",
        "declined",
        "invalid_items",
        42,
    ],
)
def test_unknown_trade_offer_lifecycle_is_not_success(lifecycle):
    result = steam_trade_offer_adapter(
        TradeOfferReaderStub(steam_trade_offer_payload(lifecycle=lifecycle))
    ).execute(steam_trade_offer_request())

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "trade_offer_state_not_proven"
    assert result.evidence is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"steam_tradeoffer_id": "offer-1"},
        steam_trade_offer_payload(items_to_receive=[{"assetid": "asset-1"}]),
        steam_trade_offer_payload(items_to_give=[], items_to_receive=[]),
        steam_trade_offer_payload(
            items_to_receive=[
                {"appid": 730, "contextid": "2", "assetid": "asset-1", "amount": 1},
                {"appid": 730, "contextid": "2", "assetid": "asset-1", "amount": 2},
            ]
        ),
    ],
)
def test_steam_trade_offer_malformed_payload_fails_closed(payload):
    result = steam_trade_offer_adapter(TradeOfferReaderStub(payload)).execute(
        steam_trade_offer_request()
    )

    assert result.status in {
        PlatformResultStatus.RESULT_UNKNOWN,
        PlatformResultStatus.MALFORMED,
    }
    assert result.is_success is False
    assert result.evidence is None


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (TimeoutError("secret timeout"), PlatformResultStatus.TIMEOUT, "timeout"),
        (RuntimeError("auth expired secret"), PlatformResultStatus.FAILURE, "network_failure"),
        (RuntimeError("network secret"), PlatformResultStatus.FAILURE, "network_failure"),
    ],
)
def test_steam_trade_offer_reader_failures_are_normalized(error, status, detail):
    result = steam_trade_offer_adapter(
        TradeOfferReaderStub(error=error)
    ).execute(steam_trade_offer_request())

    assert result.status is status
    assert result.detail == detail
    assert "secret" not in str(result.detail)
    assert result.evidence is None


def test_steam_trade_offer_auth_like_exception_is_normalized():
    class AuthExpiredError(RuntimeError):
        pass

    result = steam_trade_offer_adapter(
        TradeOfferReaderStub(error=AuthExpiredError("token secret"))
    ).execute(steam_trade_offer_request())

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "auth_failed"
    assert "token" not in str(result.detail)


def test_steam_trade_offer_reader_platform_result_is_untrusted_payload():
    reader = TradeOfferReaderStub(
        PlatformResult(
            steam_trade_offer_request(),
            PlatformResultStatus.RESULT_UNKNOWN,
        )
    )
    result = steam_trade_offer_adapter(reader).execute(
        steam_trade_offer_request()
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
