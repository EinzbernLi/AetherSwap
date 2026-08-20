from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    DEFAULT_PLATFORM_CAPABILITIES,
    BuffOrderLifecycle,
    BuffOrderLifecycleEvidence,
    OfferStateEvidence,
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.platform_readonly import BuffReadOnlyAdapter
from buff.buyer import API_HISTORY, BuffBuyer


ORDER_ID = "buff-order-1"


def request(**changes):
    value = PlatformRequest(
        purchase_id="purchase-1",
        buff_order_id=ORDER_ID,
        account_id="account-1",
        recipient_steam_id="steam-1",
        revision=1,
        capability=PlatformCapability.READ_BUFF_ORDER_LIFECYCLE,
        timeout_seconds=5.0,
    )
    return replace(value, **changes)


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


def paying_item(**changes):
    value = {
        "id": ORDER_ID,
        "state": "PAYING",
        "state_text": "等待付款",
        "pay_expire_timeout": 83,
    }
    value.update(changes)
    return value


def refunded_item(**changes):
    value = {
        "id": ORDER_ID,
        "state": "FAIL",
        "state_text": "购买失败-已退款",
        "pay_expire_timeout": -1,
        "deliver_expire_timeout": -1,
        "receive_expire_timeout": -1,
        "buyer_send_offer_timeout": -1,
        "tradeofferid": None,
        "trade_offer_url": None,
    }
    value.update(changes)
    return value


class BuffStub:
    def __init__(self, *, history_pages=None, history_error=None):
        self.history_pages = history_pages or {}
        self.history_error = history_error
        self.history_calls = []
        self.steam_calls = 0

    def get_steam_trades(self):
        self.steam_calls += 1
        raise AssertionError("lifecycle capability must not call get_steam_trades")

    def get_buy_order_history_page(self, page_num, game="csgo"):
        self.history_calls.append((page_num, game))
        if self.history_error:
            raise self.history_error
        return self.history_pages.get(page_num)


def adapter(stub):
    return BuffReadOnlyAdapter(stub, account_id="account-1")


def test_lifecycle_evidence_is_typed_exactly_and_bound_to_request():
    evidence = BuffOrderLifecycleEvidence(
        buff_order_id=ORDER_ID,
        lifecycle=BuffOrderLifecycle.PAYING,
        raw_state="PAYING",
        raw_state_text="等待付款",
        page_num=1,
    )
    result = PlatformResult(
        request(),
        PlatformResultStatus.SUCCESS,
        detail="paying",
        evidence=evidence,
    )
    assert result.evidence is evidence
    assert evidence.lifecycle.is_terminal is False

    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(),
            PlatformResultStatus.SUCCESS,
            evidence=OfferStateEvidence("offer-1", "76561198000000002"),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(),
            PlatformResultStatus.SUCCESS,
            evidence=replace(evidence, buff_order_id="other-order"),
        )


@pytest.mark.parametrize(
    "lifecycle,raw_state,raw_state_text",
    [
        (BuffOrderLifecycle.PAYING, "FAIL", "购买失败-已退款"),
        (BuffOrderLifecycle.REFUNDED, "PAYING", "等待付款"),
    ],
)
def test_lifecycle_evidence_cannot_lie_about_proven_raw_pair(
    lifecycle, raw_state, raw_state_text
):
    with pytest.raises(PlatformAdapterProtocolError):
        BuffOrderLifecycleEvidence(
            buff_order_id=ORDER_ID,
            lifecycle=lifecycle,
            raw_state=raw_state,
            raw_state_text=raw_state_text,
            page_num=1,
        )


def test_lifecycle_capability_is_not_default():
    assert PlatformCapability.READ_BUFF_ORDER_LIFECYCLE not in DEFAULT_PLATFORM_CAPABILITIES


def test_page_one_paying_is_success_and_reads_exactly_once():
    stub = BuffStub(history_pages={1: history_page(items=[paying_item()])})
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "paying"
    assert result.evidence.lifecycle is BuffOrderLifecycle.PAYING
    assert stub.history_calls == [(1, "csgo")]
    assert stub.steam_calls == 0


@pytest.mark.parametrize(
    "expires",
    [0, -0.5, -1, None, float("nan"), float("inf"), float("-inf"), True],
)
def test_paying_requires_strictly_positive_finite_timeout(expires):
    stub = BuffStub(
        history_pages={
            1: history_page(items=[paying_item(pay_expire_timeout=expires)])
        }
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_state_unproven"
    assert result.evidence is None
    assert stub.history_calls == [(1, "csgo")]


def test_page_one_refunded_is_terminal_success():
    stub = BuffStub(history_pages={1: history_page(items=[refunded_item()])})
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "refunded"
    assert result.evidence.lifecycle is BuffOrderLifecycle.REFUNDED
    assert result.evidence.lifecycle.is_terminal is True
    assert stub.history_calls == [(1, "csgo")]


@pytest.mark.parametrize(
    "item",
    [
        {"id": ORDER_ID, "state": "FAIL", "state_text": "支付失败"},
        {"id": ORDER_ID, "state": "SUCCESS", "state_text": "购买成功"},
    ],
)
def test_unproven_states_never_become_terminal_evidence(item):
    stub = BuffStub(history_pages={1: history_page(items=[item])})
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_state_unproven"
    assert result.evidence is None


def test_absence_is_bounded_to_three_explicit_pages():
    stub = BuffStub(
        history_pages={
            page: history_page(page_num=page, total_page=4)
            for page in (1, 2, 3)
        }
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"
    assert stub.history_calls == [(1, "csgo"), (2, "csgo"), (3, "csgo")]


def test_target_on_page_two_stops_before_page_three():
    stub = BuffStub(
        history_pages={
            1: history_page(page_num=1, total_page=3),
            2: history_page(
                page_num=2,
                total_page=3,
                items=[paying_item()],
            ),
        }
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence.page_num == 2
    assert stub.history_calls == [(1, "csgo"), (2, "csgo")]


def test_total_page_drift_between_valid_pages_is_tolerated():
    stub = BuffStub(
        history_pages={
            1: history_page(page_num=1, total_page=2),
            2: history_page(
                page_num=2,
                total_page=3,
                items=[refunded_item()],
            ),
        }
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence.lifecycle is BuffOrderLifecycle.REFUNDED
    assert stub.history_calls == [(1, "csgo"), (2, "csgo")]


def test_duplicate_target_id_is_malformed_and_fail_closed():
    stub = BuffStub(
        history_pages={1: history_page(items=[paying_item(), paying_item()])}
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "ambiguous_order"


@pytest.mark.parametrize(
    "payload",
    [
        history_page(items=[{"id": None}]),
        history_page(page_num=2),
        {
            "code": "OK",
            "data": {"page_num": 1, "page_size": 9, "total_page": 1, "items": []},
        },
        {
            "code": "OK",
            "data": {"page_num": 1, "page_size": 10, "total_page": 0, "items": []},
        },
        {
            "code": "OK",
            "data": {"page_num": 1, "page_size": 10, "total_page": 1, "items": {}},
        },
    ],
)
def test_malformed_history_envelopes_fail_closed(payload):
    stub = BuffStub(history_pages={1: payload})
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"
    assert stub.history_calls == [(1, "csgo")]


def test_non_ok_history_code_is_result_unknown():
    stub = BuffStub(
        history_pages={1: {"code": "NOT_OK", "data": {}}}
    )
    result = adapter(stub).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "history_non_ok"
    assert stub.history_calls == [(1, "csgo")]


class BuffVerificationRequired(Exception):
    pass


class BuffAuthExpired(Exception):
    pass


class BuffRateLimited(Exception):
    pass


class BuffRiskControlTriggered(Exception):
    pass


@pytest.mark.parametrize(
    "error,detail",
    [
        (BuffVerificationRequired(), "verification_required"),
        (BuffAuthExpired(), "auth_failed"),
        (BuffRateLimited(), "rate_limited"),
        (BuffRiskControlTriggered(), "risk_control"),
        (TimeoutError(), "timeout"),
    ],
)
def test_lifecycle_read_errors_stop_without_retry(error, detail):
    stub = BuffStub(history_error=error)
    result = adapter(stub).execute(request())
    expected_status = (
        PlatformResultStatus.TIMEOUT
        if isinstance(error, TimeoutError)
        else PlatformResultStatus.FAILURE
    )
    assert result.status is expected_status
    assert result.detail == detail
    assert stub.history_calls == [(1, "csgo")]


def test_missing_history_reader_is_unsupported_without_steam_fallback():
    class NoHistoryClient:
        def __init__(self):
            self.steam_calls = 0

        def get_steam_trades(self):
            self.steam_calls += 1
            return []

    client = NoHistoryClient()
    result = adapter(client).execute(request())
    assert result.status is PlatformResultStatus.UNSUPPORTED
    assert result.detail == "history_reader_not_available"
    assert client.steam_calls == 0


def test_buyer_history_page_uses_one_exact_get_without_write_path():
    buyer = BuffBuyer.__new__(BuffBuyer)
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK"}

    buyer._make_request = fake_make_request
    assert buyer.get_buy_order_history_page(2) == {"code": "OK"}
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == API_HISTORY
    assert kwargs["params"]["game"] == "csgo"
    assert kwargs["params"]["page_num"] == "2"
    assert kwargs["params"]["page_size"] == "10"
    assert kwargs["params"]["_"] .isdigit()


@pytest.mark.parametrize("page_num", [0, 11, True, 1.0, "1"])
def test_buyer_history_page_rejects_invalid_page_before_request(page_num):
    buyer = BuffBuyer.__new__(BuffBuyer)
    calls = []
    buyer._make_request = lambda *args, **kwargs: calls.append((args, kwargs))
    with pytest.raises(ValueError, match="page_num"):
        buyer.get_buy_order_history_page(page_num)
    assert calls == []


def test_buyer_history_page_rejects_non_csgo_before_request():
    buyer = BuffBuyer.__new__(BuffBuyer)
    calls = []
    buyer._make_request = lambda *args, **kwargs: calls.append((args, kwargs))
    with pytest.raises(ValueError, match="only csgo"):
        buyer.get_buy_order_history_page(1, game="dota2")
    assert calls == []
