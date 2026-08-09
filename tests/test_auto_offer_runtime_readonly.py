import ast
import json
from pathlib import Path

import pytest

from app.auto_offer import AUTO_OFFER_DEFAULT_ENABLED
from app.auto_offer.adapters import PlatformCapability
from app.auto_offer.contracts import AutoOfferResult, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.coordinator import ReadOnlyCoordinatorBlockedError, ReadOnlyCoordinatorConflictError
from app.auto_offer.runtime_readonly import (
    READONLY_RUNTIME_CAPABILITIES,
    ReadOnlyAutoOfferRuntime,
    ReadOnlyRuntimeConfigurationError,
    build_readonly_auto_offer_runtime,
)
from app.auto_offer.store import AutoOfferStoreStaleWriteError, StoredDelivery


STEAM_ID = "76561198000000001"
TOKEN = "runtime-token-value"
COOKIE = f"sessionid=session-value; steamLoginSecure={STEAM_ID}||{TOKEN}"
ACCOUNT_ID = "account-1"
PURCHASE_ID = "purchase-1"
BUFF_ORDER_ID = "buff-order-1"
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
GET_TRADE_OFFER_URL = "https://api.steampowered.com/IEconService/GetTradeOffer/v1/"


class Tripwire:
    def __getattribute__(self, _name):
        raise AssertionError("disabled dependency was inspected")


class FakeResponse:
    def __init__(self, *, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")


class FakeSession:
    verify = True

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET: {url}")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def post(self, *_args, **_kwargs):
        raise AssertionError("POST forbidden")


class UnsafeSession(FakeSession):
    verify = False


class FakeBuffClient:
    def __init__(self, records=None):
        self.records = records if records is not None else [buff_record()]
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
        return self.records


class FakeStore:
    def __init__(self, current, *, advance_error=None):
        self.current = current
        self.get_calls = []
        self.advance_calls = []
        self.advance_error = advance_error
        self.initialize_calls = 0

    def initialize(self):
        self.initialize_calls += 1
        raise AssertionError("factory must not initialize Store")

    def get_by_purchase_id(self, purchase_id):
        self.get_calls.append(purchase_id)
        return self.current

    def advance(self, current, target):
        self.advance_calls.append((current, target))
        if self.advance_error is not None:
            raise self.advance_error
        self.current = StoredDelivery(snapshot=target, revision=current.revision + 1)
        return self.current


def json_response(payload):
    return FakeResponse(content=json.dumps(payload).encode("utf-8"))


def html_response(value):
    return FakeResponse(content=value.encode("utf-8"))


def source_item(*, assetid=SOURCE_ASSET, contextid=SOURCE_CONTEXT, amount=1):
    return {
        "appid": APP_ID,
        "contextid": contextid,
        "assetid": assetid,
        "amount": amount,
    }


def steam_offer_payload(*, state, is_our_offer=False, items_given=None, items_received=None, completed=False):
    offer = {
        "tradeofferid": OFFER_ID,
        "accountid_other": ACCOUNT_ID_OTHER,
        "trade_offer_state": state,
        "is_our_offer": is_our_offer,
        "items_to_give": [] if items_given is None else items_given,
        "items_to_receive": [source_item()] if items_received is None else items_received,
    }
    if completed:
        offer["tradeid"] = TRADE_ID
        offer["time_updated"] = 1234.5
    return {"response": {"offer": offer}}


def receipt_html(*items):
    if not items:
        items = (
            {
                **source_item(),
                "new_contextid": NEW_CONTEXT,
                "new_assetid": NEW_ASSET,
            },
        )
    body = "\n".join(f"oItem = {json.dumps(item)};" for item in items)
    return f"<html><script>{body}</script></html>"


def inventory_payload(*, assets=None):
    if assets is None:
        assets = [
            {
                "appid": APP_ID,
                "contextid": NEW_CONTEXT,
                "assetid": NEW_ASSET,
                "amount": 1,
            }
        ]
    return {"success": 1, "assets": assets, "more_items": 0}


def buff_record():
    return {
        "buff_order_id": BUFF_ORDER_ID,
        "recipient_steam_id": STEAM_ID,
        "seller_steam_id": COUNTERPARTY_ID,
        "direction": "seller_sends_offer",
        "tradeofferid": OFFER_ID,
        "state": "pending",
    }


def snapshot(
    *,
    status=DeliveryStatus.PENDING_DIRECTION,
    mode=None,
    tradeoffer_id=None,
    delivery_error=None,
    pending_receipt=True,
    received_at=None,
    assetid=None,
):
    return DeliverySnapshot(
        purchase_id=PURCHASE_ID,
        buff_order_id=BUFF_ORDER_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        delivery_mode=mode,
        delivery_status=status,
        steam_tradeoffer_id=tradeoffer_id,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=received_at,
        delivery_error=delivery_error,
        pending_receipt=pending_receipt,
        assetid=assetid,
    )


def delivery(**changes):
    return StoredDelivery(snapshot=snapshot(**changes), revision=1)


def build_runtime(store, session, *, buff=None, recipient=STEAM_ID, cookie=COOKIE):
    return build_readonly_auto_offer_runtime(
        enabled=True,
        store=store,
        buff_client=buff or FakeBuffClient(),
        account_id=ACCOUNT_ID,
        recipient_steam_id=recipient,
        steam_cookie_string=cookie,
        steam_session=session,
        timeout_seconds=5.0,
    )


def test_default_off_is_exact_and_does_not_inspect_dependencies():
    assert AUTO_OFFER_DEFAULT_ENABLED is False
    tripwire = Tripwire()
    assert build_readonly_auto_offer_runtime(
        store=tripwire,
        buff_client=tripwire,
        account_id=tripwire,
        recipient_steam_id=tripwire,
        steam_cookie_string=tripwire,
        steam_session=tripwire,
        timeout_seconds=tripwire,
    ) is None


@pytest.mark.parametrize("enabled", [0, 1, None, "true", object()])
def test_enabled_requires_exact_bool(enabled):
    with pytest.raises(ReadOnlyRuntimeConfigurationError, match="enabled_must_be_bool"):
        build_readonly_auto_offer_runtime(enabled=enabled)


@pytest.mark.parametrize("account", [None, "", " account", "account ", 1])
def test_enabled_factory_rejects_invalid_account_id(account):
    with pytest.raises(ReadOnlyRuntimeConfigurationError, match="invalid_account_id"):
        build_readonly_auto_offer_runtime(enabled=True, account_id=account)


@pytest.mark.parametrize("recipient", [None, "", "0", "01", " 1", "+1", 1])
def test_enabled_factory_rejects_noncanonical_recipient(recipient):
    with pytest.raises(ReadOnlyRuntimeConfigurationError, match="invalid_recipient_steam_id"):
        build_readonly_auto_offer_runtime(
            enabled=True,
            account_id=ACCOUNT_ID,
            recipient_steam_id=recipient,
        )


def test_cookie_identity_mismatch_fails_before_http():
    session = FakeSession()
    other = "76561198000000002"
    with pytest.raises(ReadOnlyRuntimeConfigurationError, match="steam_identity_mismatch"):
        build_readonly_auto_offer_runtime(
            enabled=True,
            store=FakeStore(delivery()),
            buff_client=FakeBuffClient(),
            account_id=ACCOUNT_ID,
            recipient_steam_id=other,
            steam_cookie_string=COOKIE,
            steam_session=session,
        )
    assert session.calls == []


def test_unsafe_tls_session_is_rejected_without_io():
    session = UnsafeSession()
    with pytest.raises(ReadOnlyRuntimeConfigurationError, match="invalid_readonly_dependency"):
        build_runtime(FakeStore(delivery()), session)
    assert session.calls == []


def test_enabled_construction_is_zero_io_and_capabilities_are_exact():
    store = FakeStore(delivery())
    buff = FakeBuffClient()
    session = FakeSession()
    runtime = build_runtime(store, session, buff=buff)
    assert isinstance(runtime, ReadOnlyAutoOfferRuntime)
    assert runtime.capabilities == READONLY_RUNTIME_CAPABILITIES
    assert runtime.capabilities == frozenset(
        {
            PlatformCapability.READ_DELIVERY_DIRECTION,
            PlatformCapability.READ_OFFER_STATE,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        }
    )
    assert PlatformCapability.READ_INVENTORY_STATE not in runtime.capabilities
    assert PlatformCapability.SEND_OFFER not in runtime.capabilities
    assert store.initialize_calls == 0
    assert store.get_calls == []
    assert buff.calls == 0
    assert session.calls == []
    text = repr(runtime)
    assert TOKEN not in text
    assert COOKIE not in text
    assert "session-value" not in text


def test_full_seller_readonly_chain_reaches_exact_received():
    initial = delivery()
    store = FakeStore(initial)
    session = FakeSession(
        [
            json_response(steam_offer_payload(state=2)),
            json_response(steam_offer_payload(state=3)),
            json_response(steam_offer_payload(state=3, completed=True)),
            html_response(receipt_html()),
            json_response(inventory_payload()),
        ]
    )
    buff = FakeBuffClient()
    runtime = build_runtime(store, session, buff=buff)

    current = initial
    expected = [
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    ]
    for expected_status in expected:
        result = runtime.step(current)
        assert result.persisted is True
        current = result.after
        assert current.snapshot.delivery_status is expected_status

    assert current.snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
    assert current.snapshot.steam_tradeoffer_id == OFFER_ID
    assert current.snapshot.assetid == NEW_ASSET
    assert current.snapshot.received_at == 1234.5
    assert current.snapshot.pending_receipt is False
    assert buff.calls == 2
    assert len(session.calls) == 5
    assert len(store.advance_calls) == 5


def awaiting_inventory_delivery():
    return delivery(
        status=DeliveryStatus.AWAITING_INVENTORY,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        tradeoffer_id=OFFER_ID,
    )


def test_inventory_absence_remains_waiting_without_cas():
    before = awaiting_inventory_delivery()
    store = FakeStore(before)
    session = FakeSession(
        [
            json_response(steam_offer_payload(state=3, completed=True)),
            html_response(receipt_html()),
            json_response(inventory_payload(assets=[])),
        ]
    )
    result = build_runtime(store, session).step(before)
    assert result.persisted is False
    assert result.after == before
    assert result.decision.result is AutoOfferResult.WAITING
    assert store.advance_calls == []


def test_multi_item_completed_trade_is_blocked():
    second = source_item(assetid="3002")
    receipt_two = receipt_html(
        {**source_item(), "new_contextid": NEW_CONTEXT, "new_assetid": NEW_ASSET},
        {**second, "new_contextid": NEW_CONTEXT, "new_assetid": "4002"},
    )
    before = awaiting_inventory_delivery()
    store = FakeStore(before)
    session = FakeSession(
        [
            json_response(
                steam_offer_payload(
                    state=3,
                    completed=True,
                    items_received=[source_item(), second],
                )
            ),
            html_response(receipt_two),
        ]
    )
    result = build_runtime(store, session).step(before)
    assert result.persisted is False
    assert result.decision.result is AutoOfferResult.BLOCKED
    assert len(session.calls) == 2
    assert store.advance_calls == []


def test_completed_trade_with_outgoing_items_is_blocked():
    outgoing = source_item(assetid="9001")
    before = awaiting_inventory_delivery()
    store = FakeStore(before)
    session = FakeSession(
        [
            json_response(
                steam_offer_payload(
                    state=3,
                    completed=True,
                    items_given=[outgoing],
                )
            ),
            html_response(
                receipt_html(
                    {**outgoing, "new_contextid": "4", "new_assetid": "9002"},
                    {**source_item(), "new_contextid": NEW_CONTEXT, "new_assetid": NEW_ASSET},
                )
            ),
        ]
    )
    result = build_runtime(store, session).step(before)
    assert result.persisted is False
    assert result.decision.result is AutoOfferResult.BLOCKED
    assert len(session.calls) == 2
    assert store.advance_calls == []


def test_wrong_trade_offer_direction_is_blocked():
    before = delivery(
        status=DeliveryStatus.OFFER_RECEIVED,
        mode=DeliveryMode.SELLER_SENDS_OFFER,
        tradeoffer_id=OFFER_ID,
    )
    store = FakeStore(before)
    session = FakeSession([json_response(steam_offer_payload(state=2, is_our_offer=True))])
    result = build_runtime(store, session).step(before)
    assert result.persisted is False
    assert result.decision.result is AutoOfferResult.BLOCKED
    assert len(session.calls) == 1


def test_result_unknown_delivery_does_not_invoke_platform_reader():
    before = delivery(
        status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    store = FakeStore(before)
    buff = FakeBuffClient()
    session = FakeSession()
    runtime = build_runtime(store, session, buff=buff)
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="read_step_not_available"):
        runtime.step(before)
    assert buff.calls == 0
    assert session.calls == []
    assert store.advance_calls == []


def test_store_stale_write_is_not_retried():
    before = delivery()
    store = FakeStore(before, advance_error=AutoOfferStoreStaleWriteError("stale"))
    buff = FakeBuffClient()
    runtime = build_runtime(store, FakeSession(), buff=buff)
    with pytest.raises(ReadOnlyCoordinatorConflictError, match="stale_write"):
        runtime.step(before)
    assert buff.calls == 1
    assert len(store.advance_calls) == 1


def test_runtime_module_has_no_host_wiring_or_background_imports():
    path = Path(__file__).resolve().parents[1] / "app" / "auto_offer" / "runtime_readonly.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_imports = {
        "config",
        "app.config_loader",
        "app.accounts",
        "app.pipeline",
        "app.pipeline_steps",
        "app.services.workers",
        "app.receive_flow",
        "app.main",
    }
    assert imported.isdisjoint(forbidden_imports)
    for token in (
        "run_forever",
        "time.sleep",
        "ThreadPoolExecutor",
        "threading",
        "SEND_OFFER:",
        "accept_trade_offer",
        "make_offer",
        "Steam Guard",
    ):
        assert token not in source


def test_package_import_still_does_not_export_or_construct_runtime():
    import app.auto_offer as package

    assert package.AUTO_OFFER_DEFAULT_ENABLED is False
    assert not hasattr(package, "ReadOnlyAutoOfferRuntime")
    assert not hasattr(package, "build_readonly_auto_offer_runtime")
