import ast
import importlib
import math
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from app.auto_offer.adapters import (
    CompletedTradeItemEvidence,
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformAdapter,
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    RecipientInventoryItemEvidence,
    SellerOrderItemEvidence,
    SteamCompletedTradeEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)
from app.auto_offer.platform_readonly import (
    BUFF_CAPABILITIES,
    STEAM_COMPLETED_TRADE_CAPABILITIES,
    STEAM_INVENTORY_CAPABILITIES,
    STEAM_TRADE_OFFER_CAPABILITIES,
    BuffHistoricalOrderReadOnlyClient,
    BuffReadOnlyClient,
    BuffReadOnlyAdapter,
    SteamInventoryReadOnlyAdapter,
    SteamCompletedTradeReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)

WAITING_BUYER_STEAM_ID = "76561198000000001"


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


def wait_send_request():
    return request(
        capability=PlatformCapability.READ_DELIVERY_DIRECTION,
        recipient_steam_id=WAITING_BUYER_STEAM_ID,
    )


class BuffStub:
    def __init__(
        self,
        payload=None,
        error=None,
        wait_payload=None,
        wait_error=None,
        history_pages=None,
        history_error=None,
    ):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.wait_payload = wait_payload
        self.wait_error = wait_error
        self.wait_calls = []
        self.history_pages = history_pages or {}
        self.history_error = history_error
        self.history_calls = []

    def get_steam_trades(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload

    def get_buy_orders_waiting_to_send_offer(self, game="csgo", appid=730):
        self.wait_calls.append((game, appid))
        if self.wait_error:
            raise self.wait_error
        return self.wait_payload

    def get_buy_order_history_page(self, page_num, game="csgo"):
        self.history_calls.append((page_num, game))
        if self.history_error:
            raise self.history_error
        return self.history_pages.get(page_num)


class LegacyRealtimeOnlyBuffStub:
    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
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


class CompletedTradeReaderStub:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, steam_tradeoffer_id, recipient_steam_id):
        self.calls.append((steam_tradeoffer_id, recipient_steam_id))
        if self.error:
            raise self.error
        return self.payload


def buff_record(**changes):
    value = {
        "buff_order_id": "buff-order-1",
        "seller_steam_id": "76561198000000002",
        "buyer_steam_id": "steam-1",
        "state": 1,
        "tradeofferid": "offer-1",
    }
    value.update(changes)
    return value


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
        "id": "buff-order-1",
        "buyer_steam_id": "steam-1",
        "seller_steam_id": "76561198000000002",
        "tradeofferid": "history-offer-1",
        "state": "SUCCESS",
        "state_text": "已完成",
    }
    value.update(changes)
    return value


def wait_send_record(**changes):
    value = {
        "id": "buff-order-1",
        "buyer_steamid": WAITING_BUYER_STEAM_ID,
        "state_text": "等待你发起报价",
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


def seller_item_request(**changes):
    value = request(
        capability=PlatformCapability.READ_SELLER_OFFER_ITEM,
        recipient_steam_id="76561198000000001",
        steam_tradeoffer_id="offer-1",
        counterparty_steam_id="76561198000000002",
        host_goods_id=73001,
    )
    return replace(value, **changes)


def seller_item_record(**changes):
    value = {
        "buff_order_id": "buff-order-1",
        "tradeofferid": "offer-1",
        "buyer_steam_id": "76561198000000001",
        "seller_steam_id": "76561198000000002",
        "items_to_trade": [{"assetid": "asset-1", "goods_id": 73001}],
    }
    value.update(changes)
    return value


def test_exact_buff_seller_item_read_uses_one_existing_read_and_no_fallback():
    client = BuffStub(
        [seller_item_record()],
        wait_payload=[wait_send_record()],
    )
    item = seller_item_request()

    result = buff_adapter(client).execute(item)

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.request is item
    assert result.evidence == SellerOrderItemEvidence(
        buff_order_id="buff-order-1",
        steam_tradeoffer_id="offer-1",
        recipient_steam_id="76561198000000001",
        counterparty_steam_id="76561198000000002",
        goods_id=73001,
        seller_assetid="asset-1",
    )
    assert client.calls == 1
    assert client.wait_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [seller_item_record(buff_order_id="other-order")],
        [seller_item_record(), seller_item_record(tradeofferid="offer-2")],
        [
            seller_item_record(),
            seller_item_record(buff_order_id="other-order"),
        ],
        [
            seller_item_record(
                items_to_trade=[
                    {"assetid": "asset-1", "goods_id": 73001},
                    {"assetid": "asset-2", "goods_id": 73001},
                ]
            )
        ],
        [seller_item_record(buyer_steam_id="76561198000000003")],
    ],
)
def test_seller_item_absent_ambiguous_shared_multi_or_recipient_never_succeeds(
    payload,
):
    client = BuffStub(payload, wait_payload=[wait_send_record()])

    result = buff_adapter(client).execute(seller_item_request())

    assert result.status is not PlatformResultStatus.SUCCESS
    assert result.evidence is None
    assert client.calls == 1
    assert client.wait_calls == []


@pytest.mark.parametrize(
    "request_changes",
    [
        {"steam_tradeoffer_id": "offer-2"},
        {"counterparty_steam_id": "76561198000000003"},
        {"host_goods_id": 73002},
    ],
)
def test_seller_item_offer_counterparty_or_goods_mismatch_never_succeeds(
    request_changes,
):
    result = buff_adapter(BuffStub([seller_item_record()])).execute(
        seller_item_request(**request_changes)
    )
    assert result.status is not PlatformResultStatus.SUCCESS
    assert result.evidence is None


def test_seller_item_names_cannot_replace_goods_or_asset_identity():
    payload = seller_item_record(
        items_to_trade=[
            {
                "name": "asset-1",
                "market_hash_name": "asset-1",
            }
        ],
        goods_infos={
            "73001": {
                "name": "asset-1",
                "market_hash_name": "asset-1",
            }
        },
    )

    result = buff_adapter(BuffStub([payload])).execute(seller_item_request())

    assert result.status is not PlatformResultStatus.SUCCESS
    assert result.evidence is None


def steam_completed_trade_adapter(reader):
    return SteamCompletedTradeReadOnlyAdapter(
        reader,
        account_id="account-1",
        recipient_steam_id="steam-1",
    )


def steam_completed_trade_request(**changes):
    value = request(
        capability=PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        steam_tradeoffer_id="offer-1",
    )
    return replace(value, **changes)


def steam_completed_trade_payload(**changes):
    value = {
        "steam_tradeoffer_id": "offer-1",
        "steam_trade_id": "trade-1",
        "account_steam_id": "steam-1",
        "counterparty_steam_id": "steam-2",
        "completed_at": 100.0,
        "items_given": [],
        "items_received": [
            {
                "appid": 730,
                "contextid": "2",
                "assetid": "source-1",
                "amount": 1,
                "new_contextid": "3",
                "new_assetid": "new-1",
            }
        ],
        "inventory_confirmed_items": [
            {
                "appid": 730,
                "contextid": "3",
                "assetid": "new-1",
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
    steam_completed_trade_adapter(CompletedTradeReaderStub())
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

    completed = steam_completed_trade_adapter(CompletedTradeReaderStub())
    assert isinstance(completed, PlatformAdapter)
    assert completed.capabilities == STEAM_COMPLETED_TRADE_CAPABILITIES
    assert completed.capabilities == frozenset(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE}
    )
    with pytest.raises(AttributeError):
        completed.capabilities.add(PlatformCapability.SEND_OFFER)


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


@pytest.mark.parametrize(
    "payload",
    [
        buff_record(
            buff_order_id="buff-order-1",
            bill_order_id="other-order",
        ),
        buff_record(
            bill_order_id="buff-order-1",
            buff_order_id="other-order",
        ),
    ],
)
def test_conflicting_order_aliases_are_malformed_without_buyer_fallback(payload):
    client = BuffStub([payload], wait_payload=[wait_send_record()])

    result = buff_adapter(client).execute(
        wait_send_request()
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
    assert client.wait_calls == []


def test_identical_order_aliases_preserve_exact_direction_success():
    result = buff_adapter(
        BuffStub(
            [
                buff_record(
                    buff_order_id="buff-order-1",
                    bill_order_id="buff-order-1",
                )
            ]
        )
    ).execute(request(capability=PlatformCapability.READ_DELIVERY_DIRECTION))

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "seller_sends_offer"
    assert result.evidence == DeliveryDirectionEvidence(
        counterparty_steam_id="76561198000000002"
    )


def test_malformed_present_order_alias_is_malformed_without_buyer_fallback():
    client = BuffStub(
        [buff_record(bill_order_id=None)],
        wait_payload=[wait_send_record()],
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_DELIVERY_DIRECTION)
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
    assert client.wait_calls == []


@pytest.mark.parametrize("field", ("buff_order_id", "bill_order_id"))
@pytest.mark.parametrize(
    "invalid_value",
    (
        "",
        " buff-order-1",
        "buff-order-1 ",
        True,
        1.0,
        [],
        {},
    ),
)
def test_order_aliases_reject_noncanonical_raw_identifiers_without_buyer_fallback(
    field, invalid_value
):
    client = BuffStub(
        [buff_record(**{field: invalid_value})],
        wait_payload=[wait_send_record()],
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_DELIVERY_DIRECTION)
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
    assert client.wait_calls == []


def test_integer_order_alias_preserves_exact_canonical_success():
    result = buff_adapter(
        BuffStub([buff_record(buff_order_id=123)])
    ).execute(
        request(
            buff_order_id="123",
            capability=PlatformCapability.READ_DELIVERY_DIRECTION,
        )
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == DeliveryDirectionEvidence(
        counterparty_steam_id="76561198000000002"
    )


def test_conflicting_trade_offer_aliases_are_malformed_without_evidence():
    result = buff_adapter(
        BuffStub([buff_record(trade_offer_id="offer-2")])
    ).execute(request(capability=PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_identical_trade_offer_aliases_preserve_exact_offer_success():
    result = buff_adapter(
        BuffStub([buff_record(trade_offer_id="offer-1")])
    ).execute(request(capability=PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_pending"
    assert result.evidence == OfferStateEvidence("offer-1", "76561198000000002")


def test_malformed_present_trade_offer_alias_is_malformed_without_evidence():
    result = buff_adapter(
        BuffStub([buff_record(trade_offer_id=None)])
    ).execute(request(capability=PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


@pytest.mark.parametrize("field", ("tradeofferid", "trade_offer_id"))
@pytest.mark.parametrize(
    "invalid_value",
    (
        "",
        " offer-1",
        "offer-1 ",
        True,
        1.0,
        [],
        {},
    ),
)
def test_trade_offer_aliases_reject_noncanonical_raw_identifiers(
    field, invalid_value
):
    result = buff_adapter(
        BuffStub([buff_record(**{field: invalid_value})])
    ).execute(request(capability=PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_integer_trade_offer_alias_preserves_exact_canonical_success():
    result = buff_adapter(
        BuffStub([buff_record(tradeofferid=123)])
    ).execute(request(capability=PlatformCapability.READ_OFFER_STATE))

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == OfferStateEvidence("123", "76561198000000002")


def test_absent_trade_offer_aliases_preserve_order_not_proven():
    payload = buff_record()
    del payload["tradeofferid"]

    result = buff_adapter(BuffStub([payload])).execute(
        request(capability=PlatformCapability.READ_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert result.evidence is None


def test_buff_unique_direction_requires_recipient_and_proves_seller_send():
    result = buff_adapter(
        BuffStub([buff_record()])
    ).execute(request(capability=PlatformCapability.READ_DELIVERY_DIRECTION))
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "seller_sends_offer"
    assert result.evidence == DeliveryDirectionEvidence(
        counterparty_steam_id="76561198000000002"
    )

    mismatch = buff_adapter(
        BuffStub([buff_record(buyer_steam_id="other-steam")])
    ).execute(request(capability=PlatformCapability.READ_DELIVERY_DIRECTION))
    assert mismatch.status is PlatformResultStatus.FAILURE
    assert mismatch.detail == "identity_mismatch"
    assert mismatch.is_success is False


def test_seller_direction_success_is_authoritative_and_skips_buyer_fallback():
    client = BuffStub(
        [buff_record()],
        wait_payload=[wait_send_record()],
    )
    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_DELIVERY_DIRECTION)
    )
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "seller_sends_offer"
    assert client.wait_calls == []


def test_buyer_fallback_uses_exact_wait_send_id_identity_and_state():
    client = BuffStub(
        payload=[],
        wait_payload=[wait_send_record()],
    )
    result = buff_adapter(client).execute(wait_send_request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "buyer_sends_offer"
    assert result.evidence == DeliveryDirectionEvidence("buyer_sends_offer")
    assert client.wait_calls == [("csgo", 730)]


@pytest.mark.parametrize(
    "seller_payload",
    [
        None,
        [],
        [{"id": "buff-order-1"}],
        [
            buff_record(
                buyer_steam_id=WAITING_BUYER_STEAM_ID,
                direction="buyer_sends_offer",
            )
        ],
    ],
)
def test_buyer_fallback_only_follows_unknown_seller_result(seller_payload):
    client = BuffStub(seller_payload, wait_payload=[wait_send_record()])
    result = buff_adapter(client).execute(
        wait_send_request()
    )
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "buyer_sends_offer"
    assert client.wait_calls == [("csgo", 730)]


@pytest.mark.parametrize("error", [TimeoutError("slow"), RuntimeError("failed")])
def test_seller_safety_failure_is_not_masked_by_buyer_fallback(error):
    client = BuffStub(error=error, wait_payload=[wait_send_record()])
    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_DELIVERY_DIRECTION)
    )
    assert result.status in {
        PlatformResultStatus.TIMEOUT,
        PlatformResultStatus.FAILURE,
    }
    assert client.wait_calls == []


@pytest.mark.parametrize(
    "wait_payload,expected_status,expected_detail",
    [
        ([], PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"),
        ([wait_send_record(), wait_send_record()], PlatformResultStatus.MALFORMED, "ambiguous_order"),
        ([wait_send_record(id="other")], PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"),
        ([wait_send_record(state_text="等待卖家发货")], PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"),
        ([wait_send_record(buyer_steamid="76561198000000003")], PlatformResultStatus.FAILURE, "identity_mismatch"),
        ([wait_send_record(buyer_steamid=None)], PlatformResultStatus.MALFORMED, "malformed_payload"),
        ([wait_send_record()], PlatformResultStatus.SUCCESS, "buyer_sends_offer"),
    ],
)
def test_buyer_wait_send_outcomes_are_exact_and_fail_closed(
    wait_payload, expected_status, expected_detail
):
    client = BuffStub([], wait_payload=wait_payload)
    result = buff_adapter(client).execute(
        wait_send_request()
    )
    assert result.status is expected_status
    assert result.detail == expected_detail
    if expected_status is PlatformResultStatus.SUCCESS:
        assert result.evidence == DeliveryDirectionEvidence("buyer_sends_offer")


@pytest.mark.parametrize(
    "record",
    [
        {"buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": "", "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": " buff-order-1 ", "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": True, "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": 1.0, "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": ["buff-order-1"], "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
        {"id": {"value": "buff-order-1"}, "buyer_steamid": WAITING_BUYER_STEAM_ID, "state_text": "等待你发起报价"},
    ],
)
def test_buyer_wait_send_invalid_order_id_fails_closed(record):
    result = buff_adapter(BuffStub([], wait_payload=[record])).execute(
        wait_send_request()
    )
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.evidence is None


@pytest.mark.parametrize(
    "buyer_steamid",
    [
        "",
        "0",
        "076561198000000001",
        " 76561198000000001",
        "76561198000000001 ",
        "７６５６１１９８００００００１",
        76561198000000001,
        True,
        76561198000000001.0,
        ["76561198000000001"],
        {"value": "76561198000000001"},
    ],
)
def test_buyer_wait_send_requires_canonical_decimal_text_buyer_steamid(
    buyer_steamid,
):
    result = buff_adapter(
        BuffStub([], wait_payload=[wait_send_record(buyer_steamid=buyer_steamid)])
    ).execute(wait_send_request())
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_buyer_wait_send_accepts_integer_order_id_by_exact_string_conversion():
    exact_request = replace(
        wait_send_request(),
        purchase_id="purchase-7",
        buff_order_id="7",
    )
    result = buff_adapter(
        BuffStub([], wait_payload=[wait_send_record(id=7)])
    ).execute(exact_request)
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == DeliveryDirectionEvidence("buyer_sends_offer")


def test_buyer_wait_send_requires_exact_buyer_steamid_field_without_aliases():
    result = buff_adapter(
        BuffStub(
            [],
            wait_payload=[
                {
                    "id": "buff-order-1",
                    "buyer_steam_id": WAITING_BUYER_STEAM_ID,
                    "state_text": "等待你发起报价",
                }
            ],
        )
    ).execute(wait_send_request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"


def test_buff_offer_state_requires_exact_order_trade_offer_and_known_pending_state():
    result = buff_adapter(BuffStub([buff_record()])).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_pending"
    assert result.evidence == OfferStateEvidence("offer-1", "76561198000000002")

    malformed = buff_adapter(
        BuffStub([buff_record(tradeofferid=None)])
    ).execute(request())
    assert malformed.status is PlatformResultStatus.MALFORMED
    assert malformed.detail == "malformed_payload"
    assert malformed.evidence is None

    unknown_state = buff_adapter(
        BuffStub([buff_record(state="unknown")])
    ).execute(request())
    assert unknown_state.status is PlatformResultStatus.RESULT_UNKNOWN
    assert unknown_state.detail == "order_not_proven"
    assert unknown_state.evidence is None


def test_buff_offer_state_success_carries_exact_offer_and_same_record_seller():
    result = buff_adapter(BuffStub([buff_record()])).execute(request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence.steam_tradeoffer_id == "offer-1"
    assert result.evidence.counterparty_steam_id == "76561198000000002"


def test_offer_state_history_cannot_bind_when_current_read_is_unproven():
    client = BuffStub(
        [],
        history_pages={1: history_page(items=[history_record()])},
    )
    adapter = buff_adapter(client)

    result = adapter.execute(request())

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert result.evidence is None
    assert client.calls == 1
    assert client.history_calls == []
    assert not hasattr(adapter, "_recover_result_unknown_offer_state")


def test_current_pending_offer_recovery_does_not_read_history():
    client = BuffStub(
        [buff_record()],
        history_pages={1: history_page(items=[history_record()])},
    )

    result = buff_adapter(client).execute(request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert client.calls == 1
    assert client.history_calls == []


def test_legacy_realtime_only_client_preserves_public_protocol_and_realtime_reads():
    client = LegacyRealtimeOnlyBuffStub([buff_record()])

    assert isinstance(client, BuffReadOnlyClient)
    result = buff_adapter(client).execute(request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_pending"
    assert client.calls == 1


def test_legacy_realtime_only_client_history_is_unsupported_without_other_reads():
    client = LegacyRealtimeOnlyBuffStub([buff_record()])

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.UNSUPPORTED
    assert result.detail == "history_reader_not_available"
    assert client.calls == 0


def test_historical_reader_can_use_a_separate_optional_protocol_seam():
    realtime_client = LegacyRealtimeOnlyBuffStub([])
    history_client = BuffStub(
        history_pages={
            1: history_page(
                items=[history_record(tradeofferid="historical-offer-42")]
            )
        }
    )
    assert isinstance(history_client, BuffHistoricalOrderReadOnlyClient)

    result = BuffReadOnlyAdapter(
        realtime_client,
        account_id="account-1",
        historical_client=history_client,
    ).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == OfferStateEvidence(
        "historical-offer-42", "76561198000000002"
    )
    assert realtime_client.calls == 0
    assert history_client.history_calls == [(1, "csgo")]


@pytest.mark.parametrize("match_page", [1, 2, 3])
def test_historical_buyer_offer_state_reads_only_bounded_pages_and_binds_exact_offer(
    match_page,
):
    pages = {
        page_num: history_page(page_num=page_num, total_page=3)
        for page_num in range(1, 4)
    }
    pages[match_page] = history_page(
        page_num=match_page,
        total_page=3,
        items=[history_record(tradeofferid="historical-offer-42")],
    )
    client = BuffStub(history_pages=pages)

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_bound_historical"
    assert result.evidence == OfferStateEvidence(
        "historical-offer-42", "76561198000000002"
    )
    assert client.calls == 0
    assert client.history_calls == [
        (page_num, "csgo") for page_num in range(1, 4)
    ]


@pytest.mark.parametrize(
    ("page1_total", "page2_total"),
    [(3, 2), (2, 3)],
)
def test_historical_buyer_offer_state_rejects_total_page_drift(
    page1_total, page2_total
):
    client = BuffStub(
        history_pages={
            1: history_page(
                page_num=1,
                total_page=page1_total,
                items=[history_record(tradeofferid="historical-offer-42")],
            ),
            2: history_page(page_num=2, total_page=page2_total),
        }
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
    assert client.history_calls == [(1, "csgo"), (2, "csgo")]


def test_historical_total_page_drift_stops_before_hidden_later_duplicate():
    client = BuffStub(
        history_pages={
            1: history_page(
                page_num=1,
                total_page=3,
                items=[history_record(tradeofferid="historical-offer-42")],
            ),
            2: history_page(page_num=2, total_page=2),
            3: history_page(
                page_num=3,
                total_page=2,
                items=[history_record(tradeofferid="historical-offer-99")],
            ),
        }
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None
    assert client.history_calls == [(1, "csgo"), (2, "csgo")]


def test_historical_total_page_one_binds_after_only_one_page():
    client = BuffStub(
        history_pages={
            1: history_page(
                page_num=1,
                total_page=1,
                items=[history_record(tradeofferid="historical-offer-42")],
            )
        }
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == OfferStateEvidence(
        "historical-offer-42", "76561198000000002"
    )
    assert client.history_calls == [(1, "csgo")]


@pytest.mark.parametrize(
    ("duplicate_page", "second_offer"),
    [
        (2, "historical-offer-42"),
        (2, "historical-offer-99"),
        (3, "historical-offer-42"),
        (3, "historical-offer-99"),
    ],
)
def test_historical_buyer_offer_state_rejects_cross_page_duplicate_order(
    duplicate_page, second_offer
):
    pages = {
        page_num: history_page(page_num=page_num, total_page=3)
        for page_num in range(1, 4)
    }
    pages[1] = history_page(
        page_num=1,
        total_page=3,
        items=[history_record(tradeofferid="historical-offer-42")],
    )
    pages[duplicate_page] = history_page(
        page_num=duplicate_page,
        total_page=3,
        items=[history_record(tradeofferid=second_offer)],
    )
    client = BuffStub(history_pages=pages)

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "ambiguous_order"
    assert result.evidence is None
    assert client.history_calls == [
        (page_num, "csgo") for page_num in range(1, duplicate_page + 1)
    ]


def test_historical_buyer_offer_state_stops_after_three_pages_without_match():
    pages = {
        page_num: history_page(page_num=page_num, total_page=9)
        for page_num in range(1, 4)
    }
    client = BuffStub(history_pages=pages)

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert client.history_calls == [(1, "csgo"), (2, "csgo"), (3, "csgo")]


@pytest.mark.parametrize(
    ("changes", "status", "detail"),
    [
        (
            {"tradeofferid": "historical-offer-42", "trade_offer_id": "other"},
            PlatformResultStatus.MALFORMED,
            "malformed_payload",
        ),
        (
            {"tradeofferid": None},
            PlatformResultStatus.RESULT_UNKNOWN,
            "order_not_proven",
        ),
        (
            {"buyer_steam_id": "76561198000000099"},
            PlatformResultStatus.FAILURE,
            "identity_mismatch",
        ),
        (
            {"trade_offer_url": "https://steamcommunity.com/tradeoffer/other/"},
            PlatformResultStatus.MALFORMED,
            "malformed_payload",
        ),
    ],
)
def test_historical_buyer_offer_state_fails_closed_on_identity_or_offer_ambiguity(
    changes, status, detail
):
    client = BuffStub(
        history_pages={
            1: history_page(
                items=[history_record(**changes)]
            )
        }
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is status
    assert result.detail == detail
    assert result.evidence is None
    assert client.history_calls == [(1, "csgo")]


def test_historical_buyer_offer_state_accepts_valid_url_without_using_url_as_identity():
    client = BuffStub(
        history_pages={
            1: history_page(
                items=[
                    history_record(
                        tradeofferid="historical-offer-42",
                        trade_offer_url=(
                            "https://steamcommunity.com/tradeoffer/"
                            "historical-offer-42/"
                        ),
                    )
                ]
            )
        }
    )

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence.steam_tradeoffer_id == "historical-offer-42"


def test_historical_buyer_offer_state_allows_absent_counterparty_corroboration():
    record = history_record(tradeofferid="historical-offer-42")
    del record["seller_steam_id"]
    client = BuffStub(history_pages={1: history_page(items=[record])})

    result = buff_adapter(client).execute(
        request(capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == OfferStateEvidence("historical-offer-42", None)


def test_historical_buyer_offer_state_rejects_contradictory_counterparty():
    record = history_record(tradeofferid="historical-offer-42")
    del record["seller_steam_id"]
    record["buyer_steam_id"] = "76561198000000001"
    record["counterparty_steam_id"] = "76561198000000001"
    client = BuffStub(
        history_pages={
            1: history_page(items=[record])
        }
    )

    result = buff_adapter(client).execute(
        request(
            capability=PlatformCapability.READ_HISTORICAL_BUYER_OFFER_STATE,
            recipient_steam_id="76561198000000001",
        )
    )

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"


@pytest.mark.parametrize(
    "record",
    [
        {key: value for key, value in buff_record().items() if key != "seller_steam_id"},
            {
                **{
                    key: value
                    for key, value in buff_record().items()
                    if key not in {"seller_steam_id", "seller_steamid"}
                },
                "seller": "76561198000000002",
            },
    ],
)
def test_buff_offer_state_missing_exact_seller_is_unknown_without_authority(record):
    result = buff_adapter(BuffStub([record])).execute(request())

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert result.evidence is None


@pytest.mark.parametrize(
    "seller_fields",
    [
        {"seller_steam_id": "seller-1"},
        {
            "seller_steam_id": "76561198000000002",
            "seller_steamid": "76561198000000003",
        },
    ],
)
def test_buff_offer_state_malformed_or_conflicting_seller_is_malformed(seller_fields):
    result = buff_adapter(BuffStub([{**buff_record(), **seller_fields}])).execute(
        request()
    )

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_buff_offer_state_malformed_seller_is_not_hidden_by_unknown_state():
    result = buff_adapter(
        BuffStub([buff_record(state="unknown", seller_steam_id="seller-1")])
    ).execute(request())

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


def test_buff_offer_state_never_borrows_offer_from_another_order_record():
    exact_order_without_offer = buff_record()
    del exact_order_without_offer["tradeofferid"]
    unrelated = {
        "buff_order_id": "other-order",
        "tradeofferid": "offer-1",
        "seller_steam_id": "76561198000000002",
    }

    result = buff_adapter(BuffStub([exact_order_without_offer, unrelated])).execute(
        request()
    )

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert result.evidence is None


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
                OfferStateEvidence("offer-1", "76561198000000002"),
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


def test_steam_trade_offer_adapter_revalidates_forged_requests_before_reader():
    reader = TradeOfferReaderStub(steam_trade_offer_payload())
    adapter = steam_trade_offer_adapter(reader)
    source = steam_trade_offer_request()

    def forged(**changes):
        value = object.__new__(PlatformRequest)
        for name in (
            "purchase_id",
            "buff_order_id",
            "account_id",
            "recipient_steam_id",
            "revision",
            "capability",
            "timeout_seconds",
            "steam_tradeoffer_id",
        ):
            object.__setattr__(value, name, getattr(source, name))
        for name, changed in changes.items():
            if changed is None:
                object.__delattr__(value, name)
            else:
                object.__setattr__(value, name, changed)
        return value

    invalid_requests = (
        forged(revision=0),
        forged(timeout_seconds=math.nan),
        forged(capability="read_steam_trade_offer"),
        forged(steam_tradeoffer_id=None),
        forged(
            capability=PlatformCapability.READ_OFFER_STATE,
        ),
    )
    for invalid in invalid_requests:
        with pytest.raises(PlatformAdapterProtocolError):
            adapter.execute(invalid)

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
    ("lifecycle", "expected"),
    [
        ("countered", SteamTradeOfferLifecycle.COUNTERED),
        ("expired", SteamTradeOfferLifecycle.EXPIRED),
        ("canceled", SteamTradeOfferLifecycle.CANCELED),
        ("cancelled", SteamTradeOfferLifecycle.CANCELED),
        ("declined", SteamTradeOfferLifecycle.DECLINED),
        ("invalid_items", SteamTradeOfferLifecycle.INVALID_ITEMS),
        (
            "canceled_by_second_factor",
            SteamTradeOfferLifecycle.CANCELED_BY_SECOND_FACTOR,
        ),
        ("in_escrow", SteamTradeOfferLifecycle.IN_ESCROW),
    ],
)
def test_extended_trade_offer_lifecycle_is_canonical(lifecycle, expected):
    result = steam_trade_offer_adapter(
        TradeOfferReaderStub(steam_trade_offer_payload(lifecycle=lifecycle))
    ).execute(steam_trade_offer_request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == f"trade_offer_{expected.value}"
    assert result.evidence.lifecycle is expected


@pytest.mark.parametrize("lifecycle", ["confirmation_need", "unknown", 42])
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


def test_completed_trade_adapter_uses_exact_capability_and_reader_args_once():
    reader = CompletedTradeReaderStub(steam_completed_trade_payload())
    adapter = steam_completed_trade_adapter(reader)
    result = adapter.execute(steam_completed_trade_request())

    assert reader.calls == [("offer-1", "steam-1")]
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "completed_trade_proven"
    assert type(result.evidence) is SteamCompletedTradeEvidence
    assert result.evidence.steam_trade_id == "trade-1"
    assert result.evidence.items_received[0].new_assetid == "new-1"
    assert result.evidence.inventory_confirmed_items == (
        RecipientInventoryItemEvidence(
            appid=730,
            contextid="3",
            assetid="new-1",
            amount=1,
        ),
    )


def test_completed_trade_request_gate_rejects_unsupported_and_identity_mismatch_without_reader():
    reader = CompletedTradeReaderStub(steam_completed_trade_payload())
    adapter = steam_completed_trade_adapter(reader)
    for item in (
        request(capability=PlatformCapability.READ_OFFER_STATE),
        steam_completed_trade_request(account_id="other-account"),
        steam_completed_trade_request(recipient_steam_id="other-steam"),
    ):
        result = adapter.execute(item)
        assert result.status in {
            PlatformResultStatus.UNSUPPORTED,
            PlatformResultStatus.FAILURE,
        }
        assert result.evidence is None
    assert reader.calls == []


def test_completed_trade_adapter_revalidates_forged_request_before_reader():
    reader = CompletedTradeReaderStub(steam_completed_trade_payload())
    adapter = steam_completed_trade_adapter(reader)
    source = steam_completed_trade_request()

    def forged(**changes):
        value = object.__new__(PlatformRequest)
        for name in (
            "purchase_id",
            "buff_order_id",
            "account_id",
            "recipient_steam_id",
            "revision",
            "capability",
            "timeout_seconds",
            "steam_tradeoffer_id",
        ):
            object.__setattr__(value, name, getattr(source, name))
        for name, changed in changes.items():
            if changed is None:
                object.__delattr__(value, name)
            else:
                object.__setattr__(value, name, changed)
        return value

    for invalid in (
        forged(revision=0),
        forged(timeout_seconds=math.nan),
        forged(capability="read_steam_completed_trade"),
        forged(steam_tradeoffer_id=None),
    ):
        with pytest.raises(PlatformAdapterProtocolError):
            adapter.execute(invalid)
    assert reader.calls == []


def test_completed_trade_adapter_normalizes_none_and_non_mapping():
    unknown = steam_completed_trade_adapter(
        CompletedTradeReaderStub(None)
    ).execute(steam_completed_trade_request())
    malformed = steam_completed_trade_adapter(
        CompletedTradeReaderStub([])
    ).execute(steam_completed_trade_request())
    assert unknown.status is PlatformResultStatus.RESULT_UNKNOWN
    assert unknown.detail == "completed_trade_not_proven"
    assert malformed.status is PlatformResultStatus.MALFORMED
    assert malformed.detail == "malformed_payload"


@pytest.mark.parametrize(
    "payload",
    [
        {"steam_tradeoffer_id": "offer-1"},
        steam_completed_trade_payload(
            items_received=[
                {
                    "appid": 730,
                    "contextid": "2",
                    "assetid": "source-1",
                    "amount": 1,
                }
            ]
        ),
        steam_completed_trade_payload(
            inventory_confirmed_items=[
                {
                    "appid": 730,
                    "contextid": "9",
                    "assetid": "not-received",
                    "amount": 1,
                }
            ]
        ),
        steam_completed_trade_payload(items_received=[]),
    ],
)
def test_completed_trade_adapter_malformed_nested_payload_fails_closed(payload):
    result = steam_completed_trade_adapter(
        CompletedTradeReaderStub(payload)
    ).execute(steam_completed_trade_request())
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert result.evidence is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steam_tradeoffer_id", "other-offer"),
        ("account_steam_id", "other-steam"),
    ],
)
def test_completed_trade_adapter_enforces_payload_identity(field, value):
    reader = CompletedTradeReaderStub(
        steam_completed_trade_payload(**{field: value})
    )
    result = steam_completed_trade_adapter(reader).execute(
        steam_completed_trade_request()
    )
    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert result.evidence is None
    assert reader.calls == [("offer-1", "steam-1")]


def test_completed_trade_adapter_accepts_multi_item_and_inventory_subset_without_selection():
    payload = steam_completed_trade_payload(
        items_received=[
            {
                "appid": 730,
                "contextid": "2",
                "assetid": "source-2",
                "amount": 1,
                "new_contextid": "3",
                "new_assetid": "new-2",
            },
            {
                "appid": 440,
                "contextid": "2",
                "assetid": "source-1",
                "amount": 1,
                "new_contextid": "3",
                "new_assetid": "new-1",
            },
        ],
        inventory_confirmed_items=[
            {
                "appid": 440,
                "contextid": "3",
                "assetid": "new-1",
                "amount": 1,
            }
        ],
    )
    result = steam_completed_trade_adapter(
        CompletedTradeReaderStub(payload)
    ).execute(steam_completed_trade_request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert len(result.evidence.items_received) == 2
    assert len(result.evidence.inventory_confirmed_items) == 1
    assert not hasattr(result.evidence, "purchase_assetid")
    assert not hasattr(result.evidence, "selected_assetid")


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (TimeoutError("secret timeout"), PlatformResultStatus.TIMEOUT, "timeout"),
        (RuntimeError("auth expired token"), PlatformResultStatus.FAILURE, "network_failure"),
        (RuntimeError("network secret"), PlatformResultStatus.FAILURE, "network_failure"),
    ],
)
def test_completed_trade_reader_failures_are_normalized(error, status, detail):
    result = steam_completed_trade_adapter(
        CompletedTradeReaderStub(error=error)
    ).execute(steam_completed_trade_request())
    assert result.status is status
    assert result.detail == detail
    assert "secret" not in str(result.detail)
    assert "token" not in str(result.detail)
    assert result.evidence is None


def test_completed_trade_auth_like_exception_is_normalized():
    class AuthExpiredError(RuntimeError):
        pass

    result = steam_completed_trade_adapter(
        CompletedTradeReaderStub(error=AuthExpiredError("token secret"))
    ).execute(steam_completed_trade_request())
    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "auth_failed"
    assert "token" not in str(result.detail)


def test_completed_trade_adapter_has_no_state_or_platform_write_dependencies():
    module = importlib.import_module("app.auto_offer.platform_readonly")
    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "AutoOfferStore" not in module.__dict__
    assert "StoredDelivery" not in module.__dict__
    assert "DeliverySnapshot" not in module.__dict__
    assert imported.isdisjoint(
        {"sqlite3", "requests", "httpx", "aiohttp", "steam", "buff", "threading", "time"}
    )
    for banned in (
        "sleep(",
        "ThreadPoolExecutor",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
            "accept_offer(",
            "send_offer(",
    ):
        assert banned not in text
