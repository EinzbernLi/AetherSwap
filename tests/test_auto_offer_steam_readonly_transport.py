import ast
import json
import math
from pathlib import Path

import pytest
import requests

from app.auto_offer import steam_readonly_transport as transport
from app.auto_offer.adapters import (
    PlatformAdapter,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
    SteamCompletedTradeEvidence,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.platform_readonly import SteamCompletedTradeReadOnlyAdapter
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.steam_readonly_transport import (
    SteamCompletedTradeHttpReader,
    SteamReadOnlyTransportAuthError,
    SteamReadOnlyTransportError,
)
from app.auto_offer.store import StoredDelivery


STEAM_ID = "76561198000000001"
TOKEN = "token-value"
COOKIE = f"sessionid=session-value; steamLoginSecure={STEAM_ID}||{TOKEN}"
OFFER_ID = "1001"
TRADE_ID = "2001"
APP_ID = 730
SOURCE_CONTEXT = "2"
SOURCE_ASSET = "3001"
NEW_CONTEXT = "3"
NEW_ASSET = "4001"
ACCOUNT_ID_OTHER = 123
STEAM_ID64_BASE = 76561197960265728
COUNTERPARTY_ID = str(STEAM_ID64_BASE + ACCOUNT_ID_OTHER)
GET_TRADE_OFFER_URL = (
    "https://api.steampowered.com/IEconService/GetTradeOffer/v1/"
)


class FakeResponse:
    def __init__(self, *, status_code=200, content=b"", text=None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", errors="replace")


class FakeSession:
    verify = True

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET: {url}")
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(url, kwargs)
        return outcome

    def post(self, *_args, **_kwargs):
        raise AssertionError("POST is forbidden")

    def put(self, *_args, **_kwargs):
        raise AssertionError("PUT is forbidden")

    def patch(self, *_args, **_kwargs):
        raise AssertionError("PATCH is forbidden")

    def delete(self, *_args, **_kwargs):
        raise AssertionError("DELETE is forbidden")


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("real HTTP is forbidden in TASK-018 tests")

    monkeypatch.setattr(transport.requests, "Session", fail_network)
    monkeypatch.setattr(transport.requests, "get", fail_network)


def json_response(payload, *, status_code=200):
    return FakeResponse(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
    )


def html_response(html, *, status_code=200):
    return FakeResponse(status_code=status_code, content=html.encode("utf-8"))


def source_item(
    *,
    appid=APP_ID,
    contextid=SOURCE_CONTEXT,
    assetid=SOURCE_ASSET,
    amount=1,
    **extra,
):
    return {
        "appid": appid,
        "contextid": contextid,
        "assetid": assetid,
        "amount": amount,
        **extra,
    }


def offer_payload(*, items_given=None, items_received=None, **changes):
    offer = {
        "tradeofferid": OFFER_ID,
        "tradeid": TRADE_ID,
        "accountid_other": ACCOUNT_ID_OTHER,
        "trade_offer_state": 3,
        "time_updated": 1234.5,
        "is_our_offer": False,
        "items_to_give": [] if items_given is None else items_given,
        "items_to_receive": (
            [source_item()] if items_received is None else items_received
        ),
    }
    offer.update(changes)
    return {"response": {"offer": offer}}


def receipt_item(item=None, *, new_contextid=NEW_CONTEXT, new_assetid=NEW_ASSET, **extra):
    value = dict(source_item() if item is None else item)
    value.update(
        {
            "new_contextid": new_contextid,
            "new_assetid": new_assetid,
            **extra,
        }
    )
    return value


def receipt_html(*items, prefix="<html><script>", suffix="</script></html>"):
    if not items:
        items = (receipt_item(),)
    body = "\n".join(f"oItem = {json.dumps(item)};" for item in items)
    return f"{prefix}{body}{suffix}"


def inventory_payload(
    assets=None,
    *,
    more_items=0,
    last_assetid=None,
):
    payload = {
        "success": 1,
        "assets": (
            [
                {
                    "appid": APP_ID,
                    "contextid": NEW_CONTEXT,
                    "assetid": NEW_ASSET,
                    "amount": 1,
                }
            ]
            if assets is None
            else assets
        ),
        "more_items": more_items,
    }
    if last_assetid is not None:
        payload["last_assetid"] = last_assetid
    return payload


def positive_session(
    *,
    offer=None,
    receipt=None,
    inventory=None,
):
    return FakeSession(
        [
            json_response(offer or offer_payload()),
            html_response(receipt or receipt_html()),
            json_response(inventory or inventory_payload()),
        ]
    )


def reader_for(session, **kwargs):
    return SteamCompletedTradeHttpReader(COOKIE, session=session, **kwargs)


def platform_request(*, steam_tradeoffer_id=OFFER_ID):
    return PlatformRequest(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id=STEAM_ID,
        revision=1,
        capability=PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        timeout_seconds=5.0,
        steam_tradeoffer_id=steam_tradeoffer_id,
    )


def stored_delivery():
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id="purchase-1",
            buff_order_id="buff-order-1",
            account_id="account-1",
            recipient_steam_id=STEAM_ID,
            delivery_mode=DeliveryMode.SELLER_SENDS_OFFER,
            delivery_status=DeliveryStatus.AWAITING_INVENTORY,
            steam_tradeoffer_id=OFFER_ID,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision=1,
    )


def adapter_result(session):
    adapter = SteamCompletedTradeReadOnlyAdapter(
        reader_for(session),
        account_id="account-1",
        recipient_steam_id=STEAM_ID,
    )
    return adapter.execute(platform_request())


def planner_decision(session):
    before = stored_delivery()
    adapter = SteamCompletedTradeReadOnlyAdapter(
        reader_for(session),
        account_id=before.snapshot.account_id,
        recipient_steam_id=before.snapshot.recipient_steam_id,
    )
    result = adapter.execute(platform_request())
    return before, result, plan_read_evidence_transition(before, result)


def test_constructor_is_zero_io_and_binds_raw_cookie(monkeypatch):
    session = FakeSession()
    constructed = []

    def session_factory():
        constructed.append(session)
        return session

    monkeypatch.setattr(transport.requests, "Session", session_factory)
    reader = SteamCompletedTradeHttpReader(COOKIE)
    assert constructed == [session]
    assert session.calls == []
    assert reader.bound_account_steam_id == STEAM_ID


@pytest.mark.parametrize(
    "cookie",
    [
        "sessionid=x",
        "steamLoginSecure=",
        f"steamLoginSecure={STEAM_ID}||token||extra",
        f"steamLoginSecure={STEAM_ID}%7C%7Ctoken%7c%7cextra",
        f"steamLoginSecure={STEAM_ID}||token%7C%7Cextra",
        f"steamLoginSecure={STEAM_ID} ||token",
        f"steamLoginSecure= {STEAM_ID}||token",
        f"steamLoginSecure={STEAM_ID}|| token",
        f"steamLoginSecure={STEAM_ID}||token ",
        f"steamLoginSecure ={STEAM_ID}||token",
        f"steamLoginSecure=0{STEAM_ID}||token",
        f"steamLoginSecure={STEAM_ID}||token; steamLoginSecure={STEAM_ID}||other",
    ],
)
def test_constructor_rejects_missing_or_malformed_secure_cookie(cookie):
    with pytest.raises(PlatformAdapterProtocolError):
        SteamCompletedTradeHttpReader(cookie, session=FakeSession())


def test_constructor_accepts_encoded_canonical_secure_cookie():
    reader = SteamCompletedTradeHttpReader(
        f"steamLoginSecure={STEAM_ID}%7C%7C{TOKEN}",
        session=FakeSession(),
    )
    assert reader.bound_account_steam_id == STEAM_ID


def test_constructor_rejects_session_with_tls_verification_disabled():
    session = FakeSession()
    session.verify = False
    with pytest.raises(PlatformAdapterProtocolError, match="TLS verification"):
        reader_for(session)
    assert session.calls == []


def test_repr_exposes_only_bound_identity():
    value = repr(reader_for(FakeSession()))
    assert STEAM_ID in value
    for secret in (TOKEN, "steamLoginSecure", "sessionid", "Cookie"):
        assert secret not in value


@pytest.mark.parametrize(
    "tradeoffer_id",
    ["0", "01", "-1", "+1", "1.0", " 1", "1 ", "abc", 1, True],
)
def test_tradeoffer_id_requires_canonical_positive_decimal_before_network(tradeoffer_id):
    session = FakeSession()
    with pytest.raises(PlatformAdapterProtocolError):
        reader_for(session)(tradeoffer_id, STEAM_ID)
    assert session.calls == []


@pytest.mark.parametrize(
    "recipient",
    ["0", "01", "-1", "+1", "1.0", " 1", "1 ", "abc", 1, True],
)
def test_recipient_requires_canonical_positive_decimal_before_network(recipient):
    session = FakeSession()
    with pytest.raises(PlatformAdapterProtocolError):
        reader_for(session)(OFFER_ID, recipient)
    assert session.calls == []


def test_recipient_must_match_bound_cookie_before_network():
    session = FakeSession()
    with pytest.raises(PlatformAdapterProtocolError, match="authenticated Steam account"):
        reader_for(session)(OFFER_ID, str(int(STEAM_ID) + 1))
    assert session.calls == []


def test_exact_get_trade_offer_request_and_bounded_get_only_chain():
    session = positive_session()
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result is not None
    assert len(session.calls) == 3
    api_url, api_kwargs = session.calls[0]
    assert api_url == GET_TRADE_OFFER_URL
    assert api_kwargs["params"] == {
        "access_token": TOKEN,
        "tradeofferid": OFFER_ID,
        "language": "english",
    }
    assert "key" not in api_kwargs["params"]
    assert "cookies" not in api_kwargs
    for url, kwargs in session.calls:
        assert url.startswith(("https://api.steampowered.com/", "https://steamcommunity.com/"))
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (5.0, 15.0)
        assert all(math.isfinite(value) and value > 0 for value in kwargs["timeout"])


def test_returned_tradeoffer_id_must_match_exact_request():
    session = FakeSession([json_response(offer_payload(tradeofferid="1002"))])
    with pytest.raises(PlatformAdapterProtocolError, match="tradeofferid"):
        reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 1


def test_non_accepted_offer_is_not_completed_trade_proof():
    session = FakeSession([json_response(offer_payload(trade_offer_state=2))])
    assert reader_for(session)(OFFER_ID, STEAM_ID) is None
    assert len(session.calls) == 1


def test_malformed_trade_offer_state_fails_closed():
    session = FakeSession([json_response(offer_payload(trade_offer_state="3"))])
    with pytest.raises(PlatformAdapterProtocolError, match="trade_offer_state"):
        reader_for(session)(OFFER_ID, STEAM_ID)


@pytest.mark.parametrize("trade_id", [None, ""])
def test_accepted_offer_without_trade_id_is_not_completed_trade_proof(trade_id):
    session = FakeSession([json_response(offer_payload(tradeid=trade_id))])
    assert reader_for(session)(OFFER_ID, STEAM_ID) is None
    assert len(session.calls) == 1


@pytest.mark.parametrize("trade_id", ["0", "01", "-1", "+1", "1.0", 1, True])
def test_returned_trade_id_must_be_canonical_decimal(trade_id):
    session = FakeSession([json_response(offer_payload(tradeid=trade_id))])
    with pytest.raises(PlatformAdapterProtocolError, match="tradeid"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_receipt_url_uses_exact_trade_id_not_tradeoffer_id():
    session = positive_session()
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["steam_trade_id"] == TRADE_ID
    receipt_url, receipt_kwargs = session.calls[1]
    assert receipt_url == f"https://steamcommunity.com/trade/{TRADE_ID}/receipt"
    assert OFFER_ID not in receipt_url
    assert receipt_kwargs["cookies"]["steamLoginSecure"].endswith(TOKEN)
    assert receipt_kwargs["allow_redirects"] is False


def test_is_our_offer_requires_strict_bool():
    session = FakeSession([json_response(offer_payload(is_our_offer=1))])
    with pytest.raises(PlatformAdapterProtocolError, match="is_our_offer"):
        reader_for(session)(OFFER_ID, STEAM_ID)


@pytest.mark.parametrize("timestamp", [True, -1, float("nan"), float("inf"), "1"])
def test_completed_timestamp_is_exact_remote_finite_non_negative_value(timestamp):
    session = FakeSession([json_response(offer_payload(time_updated=timestamp))])
    with pytest.raises(PlatformAdapterProtocolError, match="time_updated"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_completed_timestamp_comes_from_offer_not_local_clock():
    session = positive_session(offer=offer_payload(time_updated=9876.25))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["completed_at"] == 9876.25


@pytest.mark.parametrize("account_id", [123, "123"])
def test_counterparty_account_id_maps_exactly_to_steam_id64(account_id):
    session = positive_session(offer=offer_payload(accountid_other=account_id))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["counterparty_steam_id"] == str(STEAM_ID64_BASE + 123)


@pytest.mark.parametrize("account_id", [0, -1, "01", "0", 2**32, True, "abc"])
def test_counterparty_account_id_rejects_invalid_or_out_of_range_values(account_id):
    session = FakeSession([json_response(offer_payload(accountid_other=account_id))])
    with pytest.raises(PlatformAdapterProtocolError, match="accountid_other"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_counterparty_cannot_equal_bound_recipient():
    account_id = int(STEAM_ID) - STEAM_ID64_BASE
    session = FakeSession([json_response(offer_payload(accountid_other=account_id))])
    with pytest.raises(PlatformAdapterProtocolError, match="counterparty"):
        reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("appid", 0),
        ("appid", False),
        ("appid", "01"),
        ("amount", 0),
        ("amount", False),
        ("amount", "01"),
        ("contextid", " 2"),
        ("assetid", "3001 "),
    ],
)
def test_offer_source_items_reject_malformed_identity_fields(field, value):
    item = source_item()
    item[field] = value
    session = FakeSession([json_response(offer_payload(items_received=[item]))])
    with pytest.raises(PlatformAdapterProtocolError):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_offer_source_items_accept_canonical_decimal_appid_and_amount():
    item = source_item(appid="730", amount="1")
    session = positive_session(offer=offer_payload(items_received=[item]))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["items_received"][0]["appid"] == 730
    assert result["items_received"][0]["amount"] == 1


def test_offer_source_item_missing_field_fails_closed():
    item = source_item()
    del item["assetid"]
    session = FakeSession([json_response(offer_payload(items_received=[item]))])
    with pytest.raises(PlatformAdapterProtocolError):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_offer_source_duplicate_identity_fails_closed():
    session = FakeSession(
        [json_response(offer_payload(items_received=[source_item(), source_item()]))]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="duplicate source"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_offer_metadata_never_enters_normalized_identity():
    item = source_item(
        classid="class-x",
        instanceid="instance-x",
        market_hash_name="name-x",
        name="display-x",
        price="999",
    )
    session = positive_session(offer=offer_payload(items_received=[item]))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["items_received"][0] == {
        "appid": APP_ID,
        "contextid": SOURCE_CONTEXT,
        "assetid": SOURCE_ASSET,
        "amount": 1,
        "new_contextid": NEW_CONTEXT,
        "new_assetid": NEW_ASSET,
    }


def test_receipt_balanced_scanner_handles_braces_escaped_quote_and_html_noise():
    item = receipt_item(note='brace { } and escaped quote " remains data')
    html = receipt_html(
        item,
        prefix="<html>before<script>var ignored = {};\n",
        suffix="\nafter</script></html>",
    )
    session = positive_session(receipt=html)
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["items_received"][0]["new_assetid"] == NEW_ASSET


def test_receipt_parser_handles_multiple_objects_and_ignores_unrelated_item():
    unrelated = receipt_item(
        source_item(assetid="9999"), new_assetid="9998", name="same-name"
    )
    session = positive_session(receipt=receipt_html(unrelated, receipt_item()))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(result["items_received"]) == 1
    assert result["items_received"][0]["assetid"] == SOURCE_ASSET


@pytest.mark.parametrize(
    "html",
    [
        '<script>oItem = {"appid": 730',
        '<script>oItem = {not-valid-json};</script>',
    ],
)
def test_malformed_or_unterminated_receipt_fails_closed(html):
    session = FakeSession(
        [json_response(offer_payload()), html_response(html)]
    )
    with pytest.raises(SteamReadOnlyTransportError, match="malformed_receipt"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_receipt_body_size_limit_is_enforced():
    session = FakeSession(
        [json_response(offer_payload()), html_response(receipt_html())]
    )
    with pytest.raises(SteamReadOnlyTransportError, match="too_large"):
        reader_for(session, max_receipt_bytes=10)(OFFER_ID, STEAM_ID)


def test_receipt_object_count_limit_is_enforced():
    unrelated = receipt_item(source_item(assetid="9999"), new_assetid="9998")
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html(receipt_item(), unrelated)),
        ]
    )
    with pytest.raises(SteamReadOnlyTransportError, match="object_limit"):
        reader_for(session, max_receipt_objects=1)(OFFER_ID, STEAM_ID)


def test_receipt_without_any_item_returns_none():
    session = FakeSession(
        [json_response(offer_payload()), html_response("<html>no receipt objects</html>")]
    )
    assert reader_for(session)(OFFER_ID, STEAM_ID) is None
    assert len(session.calls) == 2


def test_exact_source_to_new_asset_mapping_allows_unchanged_assetid():
    item = receipt_item(new_assetid=SOURCE_ASSET)
    inventory = inventory_payload(
        assets=[
            {
                "appid": APP_ID,
                "contextid": NEW_CONTEXT,
                "assetid": SOURCE_ASSET,
                "amount": 1,
            }
        ]
    )
    session = positive_session(receipt=receipt_html(item), inventory=inventory)
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["items_received"][0]["new_assetid"] == SOURCE_ASSET
    assert result["inventory_confirmed_items"][0]["assetid"] == SOURCE_ASSET


def test_missing_exact_source_receipt_mapping_returns_none_without_fallback():
    unrelated = receipt_item(
        source_item(assetid="9999"),
        new_assetid="9998",
        name="same-name",
        market_hash_name="same-name",
        classid="same-class",
        instanceid="same-instance",
        price="1",
    )
    session = FakeSession(
        [json_response(offer_payload()), html_response(receipt_html(unrelated))]
    )
    assert reader_for(session)(OFFER_ID, STEAM_ID) is None
    assert len(session.calls) == 2


def test_duplicate_exact_receipt_match_fails_closed():
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html(receipt_item(), receipt_item(new_assetid="4002"))),
        ]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="duplicate source"):
        reader_for(session)(OFFER_ID, STEAM_ID)


@pytest.mark.parametrize("missing", ["new_contextid", "new_assetid"])
def test_partial_receipt_mapping_fails_closed(missing):
    item = receipt_item()
    del item[missing]
    session = FakeSession(
        [json_response(offer_payload()), html_response(receipt_html(item))]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="incomplete"):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_recipient_inventory_request_and_exact_positive_confirmation():
    session = positive_session()
    result = reader_for(session, inventory_count=77)(OFFER_ID, STEAM_ID)
    inventory_url, kwargs = session.calls[2]
    assert inventory_url == (
        f"https://steamcommunity.com/inventory/{STEAM_ID}/{APP_ID}/{NEW_CONTEXT}"
    )
    assert kwargs["params"] == {"l": "english", "count": 77}
    assert kwargs["cookies"]["steamLoginSecure"].endswith(TOKEN)
    assert kwargs["allow_redirects"] is False
    assert result["inventory_confirmed_items"] == [
        {
            "appid": APP_ID,
            "contextid": NEW_CONTEXT,
            "assetid": NEW_ASSET,
            "amount": 1,
        }
    ]


@pytest.mark.parametrize(
    "malicious_context",
    ["3/evil", "../3", "3?x=1", "3#fragment", "3%2Fevil", "3\\evil", "３"],
)
def test_malicious_receipt_context_is_blocked_before_inventory_get(
    malicious_context,
):
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(
                receipt_html(receipt_item(new_contextid=malicious_context))
            ),
        ]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="safe URL path segment"):
        reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 2


def test_safe_unreserved_inventory_context_remains_supported():
    contextid = "context-3._~"
    session = positive_session(
        receipt=receipt_html(receipt_item(new_contextid=contextid)),
        inventory=inventory_payload(
            assets=[
                {
                    "appid": APP_ID,
                    "contextid": contextid,
                    "assetid": NEW_ASSET,
                    "amount": 1,
                }
            ]
        ),
    )
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["items_received"][0]["new_contextid"] == contextid
    assert result["inventory_confirmed_items"][0]["contextid"] == contextid
    assert session.calls[2][0].endswith(f"/{APP_ID}/{contextid}")


@pytest.mark.parametrize(
    "url",
    [
        "http://steamcommunity.com/trade/2001/receipt",
        "https://example.com/trade/2001/receipt",
        "https://steamcommunity.com/login",
        "https://steamcommunity.com/trade/2001/receipt?x=1",
        f"https://steamcommunity.com/inventory/{STEAM_ID}/{APP_ID}/3/extra",
    ],
)
def test_runtime_get_rejects_non_allowlisted_url_before_session_call(url):
    session = FakeSession()
    reader = reader_for(session)
    with pytest.raises(PlatformAdapterProtocolError, match="not allowlisted"):
        reader._get(url, community=True)
    assert session.calls == []


@pytest.mark.parametrize(
    "asset",
    [
        {"appid": APP_ID, "contextid": "4", "assetid": NEW_ASSET, "amount": 1},
        {"appid": APP_ID, "contextid": NEW_CONTEXT, "assetid": NEW_ASSET, "amount": 2},
        {"appid": 440, "contextid": NEW_CONTEXT, "assetid": NEW_ASSET, "amount": 1},
    ],
)
def test_inventory_requires_exact_app_context_asset_and_amount(asset):
    session = positive_session(inventory=inventory_payload(assets=[asset]))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result is not None
    assert result["inventory_confirmed_items"] == []


def test_inventory_absence_returns_completed_trade_with_empty_confirmation():
    session = positive_session(inventory=inventory_payload(assets=[]))
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result is not None
    assert len(result["items_received"]) == 1
    assert result["inventory_confirmed_items"] == []


def test_inventory_pagination_uses_exact_monotonic_cursor_and_stops_when_found():
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html()),
            json_response(inventory_payload(assets=[], more_items=1, last_assetid="100")),
            json_response(inventory_payload(more_items=1)),
            AssertionError("target page must stop pagination"),
        ]
    )
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert result["inventory_confirmed_items"][0]["assetid"] == NEW_ASSET
    assert len(session.calls) == 4
    assert session.calls[2][1]["params"] == {"l": "english", "count": 5000}
    assert session.calls[3][1]["params"] == {
        "l": "english",
        "count": 5000,
        "start_assetid": "100",
    }


@pytest.mark.parametrize(
    "second_cursor,match",
    [("100", "pagination_cycle"), ("99", "pagination_cycle")],
)
def test_inventory_repeated_or_decreasing_cursor_fails_closed(second_cursor, match):
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html()),
            json_response(inventory_payload(assets=[], more_items=1, last_assetid="100")),
            json_response(
                inventory_payload(assets=[], more_items=1, last_assetid=second_cursor)
            ),
        ]
    )
    with pytest.raises(SteamReadOnlyTransportError, match=match):
        reader_for(session)(OFFER_ID, STEAM_ID)


@pytest.mark.parametrize("cursor", [None, "0", "01", "abc", True])
def test_inventory_more_items_requires_valid_cursor(cursor):
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html()),
            json_response(inventory_payload(assets=[], more_items=1, last_assetid=cursor)),
        ]
    )
    with pytest.raises((SteamReadOnlyTransportError, PlatformAdapterProtocolError)):
        reader_for(session)(OFFER_ID, STEAM_ID)


def test_inventory_max_page_bound_fails_closed_without_retry():
    session = FakeSession(
        [
            json_response(offer_payload()),
            html_response(receipt_html()),
            json_response(inventory_payload(assets=[], more_items=1, last_assetid="100")),
        ]
    )
    with pytest.raises(SteamReadOnlyTransportError, match="page_limit"):
        reader_for(session, max_inventory_pages=1)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 3


def test_outgoing_item_shape_skips_inventory_but_keeps_normalized_evidence():
    given = source_item(assetid="3101")
    given_receipt = receipt_item(given, new_assetid="4101")
    session = FakeSession(
        [
            json_response(offer_payload(items_given=[given])),
            html_response(receipt_html(given_receipt, receipt_item())),
        ]
    )
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 2
    assert len(result["items_given"]) == 1
    assert result["inventory_confirmed_items"] == []


def test_multi_item_shape_skips_inventory_without_selecting_purchase_item():
    second = source_item(assetid="3002")
    session = FakeSession(
        [
            json_response(offer_payload(items_received=[source_item(), second])),
            html_response(
                receipt_html(receipt_item(), receipt_item(second, new_assetid="4002"))
            ),
        ]
    )
    result = reader_for(session)(OFFER_ID, STEAM_ID)
    assert len(session.calls) == 2
    assert len(result["items_received"]) == 2
    assert result["inventory_confirmed_items"] == []
    assert "purchase_assetid" not in result
    assert "selected_assetid" not in result


@pytest.mark.parametrize("status", [401, 403, 302, 307])
def test_auth_and_redirect_failures_are_sanitized(status):
    session = FakeSession(
        [FakeResponse(status_code=status, content=f"secret {TOKEN}".encode())]
    )
    with pytest.raises(SteamReadOnlyTransportAuthError) as caught:
        reader_for(session)(OFFER_ID, STEAM_ID)
    message = str(caught.value)
    assert TOKEN not in message
    assert "steamLoginSecure" not in message
    assert "sessionid" not in message


@pytest.mark.parametrize("error", [requests.Timeout(f"secret {TOKEN}"), TimeoutError(TOKEN)])
def test_timeout_maps_to_existing_sanitized_timeout_path(error):
    session = FakeSession([error])
    with pytest.raises(PlatformAdapterTimeoutError) as caught:
        reader_for(session)(OFFER_ID, STEAM_ID)
    assert str(caught.value) == "steam_read_timeout"
    assert TOKEN not in str(caught.value)


def test_generic_network_exception_is_sanitized():
    session = FakeSession([RuntimeError(f"URL?access_token={TOKEN}; {COOKIE}")])
    with pytest.raises(SteamReadOnlyTransportError) as caught:
        reader_for(session)(OFFER_ID, STEAM_ID)
    assert str(caught.value) == "steam_read_failure"
    for secret in (TOKEN, "steamLoginSecure", "sessionid", COOKIE):
        assert secret not in str(caught.value)


def test_source_has_no_forbidden_transport_behaviors():
    path = Path(transport.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not ({"threading", "time"} & imported)
    forbidden_attributes = {
        "post",
        "put",
        "patch",
        "delete",
        "sleep",
        "send_offer",
        "accept_trade_offer",
        "make_offer",
    }
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    for call in calls:
        if isinstance(call.func, ast.Attribute):
            assert call.func.attr not in forbidden_attributes
        if isinstance(call.func, ast.Name):
            assert call.func.id not in {"sleep", "ThreadPoolExecutor"}
        for keyword in call.keywords:
            assert not (
                keyword.arg == "verify"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            )
    forbidden_source_tokens = (
        "GetTradeHistory",
        "GetTradeOffers",
        "shared_secret",
        "identity_secret",
    )
    for token in forbidden_source_tokens:
        assert token not in source


def test_mapping_integrates_with_completed_trade_adapter_as_typed_success():
    session = positive_session()
    adapter = SteamCompletedTradeReadOnlyAdapter(
        reader_for(session),
        account_id="account-1",
        recipient_steam_id=STEAM_ID,
    )
    assert isinstance(adapter, PlatformAdapter)
    result = adapter.execute(platform_request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert type(result.evidence) is SteamCompletedTradeEvidence
    evidence = result.evidence
    assert evidence.steam_tradeoffer_id == OFFER_ID
    assert evidence.steam_trade_id == TRADE_ID
    assert evidence.account_steam_id == STEAM_ID
    assert evidence.counterparty_steam_id == COUNTERPARTY_ID
    assert evidence.completed_at == 1234.5
    assert evidence.items_received[0].assetid == SOURCE_ASSET
    assert evidence.items_received[0].new_contextid == NEW_CONTEXT
    assert evidence.items_received[0].new_assetid == NEW_ASSET
    assert evidence.inventory_confirmed_items[0].assetid == NEW_ASSET


def test_task017_confirmed_single_item_is_complete_received_without_store_write():
    before, result, decision = planner_decision(positive_session())
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.COMPLETE
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.RECEIVED
    assert decision.target.assetid == NEW_ASSET
    assert decision.target.received_at == 1234.5
    assert decision.target.pending_receipt is False
    assert before.snapshot.delivery_status is DeliveryStatus.AWAITING_INVENTORY
    assert before.revision == 1


def test_task017_single_unconfirmed_item_remains_waiting():
    session = positive_session(inventory=inventory_payload(assets=[]))
    _before, result, decision = planner_decision(session)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.WAITING
    assert decision.target is None
    assert decision.detail == "recipient_inventory_not_confirmed"


def test_task017_multi_item_remains_blocked_without_purchase_selection():
    second = source_item(assetid="3002")
    session = FakeSession(
        [
            json_response(offer_payload(items_received=[source_item(), second])),
            html_response(
                receipt_html(receipt_item(), receipt_item(second, new_assetid="4002"))
            ),
        ]
    )
    _before, result, decision = planner_decision(session)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.detail == "purchase_asset_attribution_ambiguous"


def test_task017_outgoing_item_remains_blocked():
    given = source_item(assetid="3101")
    session = FakeSession(
        [
            json_response(offer_payload(items_given=[given])),
            html_response(
                receipt_html(
                    receipt_item(given, new_assetid="4101"),
                    receipt_item(),
                )
            ),
        ]
    )
    _before, result, decision = planner_decision(session)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.detail == "completed_trade_outgoing_items_present"


# TASK-019 exact Steam Trade Offer lifecycle transport coverage.
def trade_offer_reader_for(session, **kwargs):
    from app.auto_offer.steam_readonly_transport import SteamTradeOfferHttpReader

    return SteamTradeOfferHttpReader(COOKIE, session=session, **kwargs)


def trade_offer_request(delivery):
    return PlatformRequest(
        purchase_id=delivery.snapshot.purchase_id,
        buff_order_id=delivery.snapshot.buff_order_id,
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
        revision=delivery.revision,
        capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
        timeout_seconds=5.0,
        steam_tradeoffer_id=delivery.snapshot.steam_tradeoffer_id,
    )


def trade_offer_delivery(status, *, mode=DeliveryMode.SELLER_SENDS_OFFER):
    buyer = mode is DeliveryMode.BUYER_SENDS_OFFER
    return StoredDelivery(
        snapshot=DeliverySnapshot(
            purchase_id="purchase-1",
            buff_order_id="buff-order-1",
            account_id="account-1",
            recipient_steam_id=STEAM_ID,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=OFFER_ID,
            offer_attempted_at=10.0 if buyer else None,
            offer_sent_at=11.0 if buyer else None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision=1,
    )


def trade_offer_plan(session, delivery):
    from app.auto_offer.platform_readonly import SteamTradeOfferReadOnlyAdapter

    adapter = SteamTradeOfferReadOnlyAdapter(
        trade_offer_reader_for(session),
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
    )
    result = adapter.execute(trade_offer_request(delivery))
    return result, plan_read_evidence_transition(delivery, result)


def test_trade_offer_reader_constructor_zero_io_and_reuses_strict_cookie_boundary(monkeypatch):
    from app.auto_offer.steam_readonly_transport import SteamTradeOfferHttpReader

    session = FakeSession()
    created = []

    def session_factory():
        created.append(session)
        return session

    monkeypatch.setattr(transport.requests, "Session", session_factory)
    reader = SteamTradeOfferHttpReader(COOKIE)
    assert created == [session]
    assert session.calls == []
    assert reader.bound_account_steam_id == STEAM_ID
    assert TOKEN not in repr(reader)

    bad = FakeSession()
    bad.verify = False
    with pytest.raises(PlatformAdapterProtocolError, match="TLS verification"):
        SteamTradeOfferHttpReader(COOKIE, session=bad)
    assert bad.calls == []


@pytest.mark.parametrize(
    "tradeoffer_id",
    ["0", "01", "-1", "+1", "1.0", " 1", "1 ", "abc", 1, True],
)
def test_trade_offer_reader_validates_exact_id_before_network(tradeoffer_id):
    session = FakeSession()
    with pytest.raises(PlatformAdapterProtocolError):
        trade_offer_reader_for(session)(tradeoffer_id)
    assert session.calls == []


@pytest.mark.parametrize(
    "url,community",
    [
        (
            f"https://steamcommunity.com/trade/{TRADE_ID}/receipt",
            True,
        ),
        (
            f"https://steamcommunity.com/inventory/{STEAM_ID}/{APP_ID}/{NEW_CONTEXT}",
            True,
        ),
        (
            GET_TRADE_OFFER_URL,
            True,
        ),
    ],
)
def test_trade_offer_reader_rejects_non_offer_routes_before_session_io(
    url,
    community,
):
    session = FakeSession()
    reader = trade_offer_reader_for(session)

    with pytest.raises(
        PlatformAdapterProtocolError,
        match="not allowlisted",
    ):
        reader._get(url, community=community)

    assert session.calls == []


def test_trade_offer_reader_active_uses_one_exact_bounded_get_only_request():
    session = FakeSession([json_response(offer_payload(trade_offer_state=2))])
    result = trade_offer_reader_for(session)(OFFER_ID)
    assert result["steam_tradeoffer_id"] == OFFER_ID
    assert result["account_steam_id"] == STEAM_ID
    assert result["counterparty_steam_id"] == COUNTERPARTY_ID
    assert result["is_our_offer"] is False
    assert result["lifecycle"] == "active"
    assert result["items_to_give"] == []
    assert result["items_to_receive"] == [source_item()]
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == GET_TRADE_OFFER_URL
    assert kwargs == {
        "params": {
            "access_token": TOKEN,
            "tradeofferid": OFFER_ID,
            "language": "english",
        },
        "timeout": (5.0, 15.0),
        "allow_redirects": False,
    }


def test_trade_offer_reader_accepted_does_not_require_trade_id_or_receipt():
    payload = offer_payload(trade_offer_state=3, tradeid=None, time_updated=None)
    session = FakeSession([json_response(payload)])
    result = trade_offer_reader_for(session)(OFFER_ID)
    assert result["lifecycle"] == "accepted"
    assert "steam_trade_id" not in result
    assert "completed_at" not in result
    assert len(session.calls) == 1


@pytest.mark.parametrize("state", [1, 4, 5, 6, 7, 8, 9, 10, 11, 99])
def test_trade_offer_reader_non_positive_lifecycle_returns_no_evidence(state):
    session = FakeSession([json_response(offer_payload(trade_offer_state=state))])
    assert trade_offer_reader_for(session)(OFFER_ID) is None
    assert len(session.calls) == 1


@pytest.mark.parametrize("state", [None, "2", True, 2.0])
def test_trade_offer_reader_malformed_lifecycle_type_fails_closed(state):
    session = FakeSession([json_response(offer_payload(trade_offer_state=state))])
    with pytest.raises(PlatformAdapterProtocolError, match="trade_offer_state"):
        trade_offer_reader_for(session)(OFFER_ID)


def test_trade_offer_reader_exact_returned_identity_is_required():
    session = FakeSession([json_response(offer_payload(tradeofferid="1002", trade_offer_state=2))])
    with pytest.raises(PlatformAdapterProtocolError, match="tradeofferid"):
        trade_offer_reader_for(session)(OFFER_ID)
    assert len(session.calls) == 1


def test_trade_offer_reader_strict_direction_and_counterparty_identity():
    session = FakeSession([json_response(offer_payload(trade_offer_state=2, is_our_offer=1))])
    with pytest.raises(PlatformAdapterProtocolError, match="is_our_offer"):
        trade_offer_reader_for(session)(OFFER_ID)

    same_account = int(STEAM_ID) - STEAM_ID64_BASE
    session = FakeSession(
        [json_response(offer_payload(trade_offer_state=2, accountid_other=same_account))]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="counterparty"):
        trade_offer_reader_for(session)(OFFER_ID)


def test_trade_offer_reader_canonicalizes_exact_items_and_drops_metadata():
    later = source_item(assetid="3002", market_hash_name="ignored", price="99")
    earlier = source_item(assetid="3001", name="ignored", classid="ignored")
    session = FakeSession(
        [
            json_response(
                offer_payload(
                    trade_offer_state=2,
                    items_received=[later, earlier],
                )
            )
        ]
    )
    result = trade_offer_reader_for(session)(OFFER_ID)
    assert result["items_to_receive"] == [source_item(assetid="3001"), source_item(assetid="3002")]
    assert all(set(item) == {"appid", "contextid", "assetid", "amount"} for item in result["items_to_receive"])


def test_trade_offer_reader_duplicate_or_empty_item_shape_fails_closed():
    duplicate = FakeSession(
        [
            json_response(
                offer_payload(
                    trade_offer_state=2,
                    items_received=[source_item(), source_item()],
                )
            )
        ]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="duplicate source"):
        trade_offer_reader_for(duplicate)(OFFER_ID)

    empty = FakeSession(
        [
            json_response(
                offer_payload(
                    trade_offer_state=2,
                    items_given=[],
                    items_received=[],
                )
            )
        ]
    )
    with pytest.raises(PlatformAdapterProtocolError, match="at least one item"):
        trade_offer_reader_for(empty)(OFFER_ID)


def test_trade_offer_reader_maps_through_existing_adapter_to_typed_active_evidence():
    from app.auto_offer.adapters import SteamTradeOfferEvidence, SteamTradeOfferLifecycle
    from app.auto_offer.platform_readonly import SteamTradeOfferReadOnlyAdapter

    delivery = trade_offer_delivery(DeliveryStatus.OFFER_RECEIVED)
    session = FakeSession([json_response(offer_payload(trade_offer_state=2))])
    adapter = SteamTradeOfferReadOnlyAdapter(
        trade_offer_reader_for(session),
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
    )
    result = adapter.execute(trade_offer_request(delivery))
    assert result.status is PlatformResultStatus.SUCCESS
    assert type(result.evidence) is SteamTradeOfferEvidence
    assert result.evidence.lifecycle is SteamTradeOfferLifecycle.ACTIVE
    assert result.evidence.steam_tradeoffer_id == OFFER_ID
    assert result.evidence.account_steam_id == STEAM_ID
    assert result.evidence.counterparty_steam_id == COUNTERPARTY_ID
    assert result.evidence.items_to_receive[0].assetid == SOURCE_ASSET


def test_trade_offer_reader_non_positive_state_remains_result_unknown_via_adapter():
    from app.auto_offer.platform_readonly import SteamTradeOfferReadOnlyAdapter

    delivery = trade_offer_delivery(DeliveryStatus.OFFER_RECEIVED)
    session = FakeSession([json_response(offer_payload(trade_offer_state=6))])
    adapter = SteamTradeOfferReadOnlyAdapter(
        trade_offer_reader_for(session),
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
    )
    result = adapter.execute(trade_offer_request(delivery))
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.evidence is None
    assert result.detail == "trade_offer_not_proven"


def test_task014_seller_offer_received_active_proposes_offer_confirmed():
    delivery = trade_offer_delivery(DeliveryStatus.OFFER_RECEIVED)
    session = FakeSession([json_response(offer_payload(trade_offer_state=2, is_our_offer=False))])
    result, decision = trade_offer_plan(session, delivery)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.WAITING
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert decision.detail == "trade_offer_confirmed_active"


def test_task014_buyer_offer_sent_exact_direction_active_preserves_existing_gate():
    delivery = trade_offer_delivery(
        DeliveryStatus.OFFER_SENT,
        mode=DeliveryMode.BUYER_SENDS_OFFER,
    )
    session = FakeSession([json_response(offer_payload(trade_offer_state=2, is_our_offer=True))])
    result, decision = trade_offer_plan(session, delivery)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.WAITING
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED


def test_task014_offer_confirmed_accepted_proposes_awaiting_inventory():
    delivery = trade_offer_delivery(DeliveryStatus.OFFER_CONFIRMED)
    session = FakeSession([json_response(offer_payload(trade_offer_state=3, is_our_offer=False))])
    result, decision = trade_offer_plan(session, delivery)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.WAITING
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.AWAITING_INVENTORY
    assert decision.detail == "trade_offer_accepted"


def test_task014_wrong_direction_remains_blocked():
    delivery = trade_offer_delivery(DeliveryStatus.OFFER_RECEIVED)
    session = FakeSession([json_response(offer_payload(trade_offer_state=2, is_our_offer=True))])
    result, decision = trade_offer_plan(session, delivery)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.detail == "trade_offer_direction_mismatch"


def test_task014_outgoing_item_safety_gate_remains_blocked():
    delivery = trade_offer_delivery(DeliveryStatus.OFFER_RECEIVED)
    given = source_item(assetid="3101")
    session = FakeSession(
        [
            json_response(
                offer_payload(
                    trade_offer_state=2,
                    is_our_offer=False,
                    items_given=[given],
                    items_received=[source_item()],
                )
            )
        ]
    )
    result, decision = trade_offer_plan(session, delivery)
    assert result.status is PlatformResultStatus.SUCCESS
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.detail == "trade_offer_outgoing_items_present"


def test_trade_offer_reader_auth_timeout_and_network_errors_are_sanitized():
    for response in (
        FakeResponse(status_code=401, content=f"secret {TOKEN}".encode()),
        FakeResponse(status_code=302, content=f"secret {TOKEN}".encode()),
    ):
        with pytest.raises(SteamReadOnlyTransportAuthError) as caught:
            trade_offer_reader_for(FakeSession([response]))(OFFER_ID)
        assert TOKEN not in str(caught.value)

    with pytest.raises(PlatformAdapterTimeoutError) as caught:
        trade_offer_reader_for(FakeSession([requests.Timeout(TOKEN)]))(OFFER_ID)
    assert str(caught.value) == "steam_read_timeout"
    assert TOKEN not in str(caught.value)

    with pytest.raises(SteamReadOnlyTransportError) as caught:
        trade_offer_reader_for(FakeSession([RuntimeError(COOKIE)]))(OFFER_ID)
    assert str(caught.value) == "steam_read_failure"
    assert TOKEN not in str(caught.value)


def test_trade_offer_reader_json_bound_is_enforced_without_retry():
    session = FakeSession([json_response(offer_payload(trade_offer_state=2))])
    with pytest.raises(SteamReadOnlyTransportError, match="too_large"):
        trade_offer_reader_for(session, max_json_bytes=10)(OFFER_ID)
    assert len(session.calls) == 1
