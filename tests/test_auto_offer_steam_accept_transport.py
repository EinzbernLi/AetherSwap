from __future__ import annotations

import pytest
import requests

from app.auto_offer.adapters import PlatformAdapterProtocolError
from app.auto_offer.platform_accept import (
    AcceptOfferPreflightError,
    AcceptOfferWriteResultUnknown,
)
from app.auto_offer.steam_accept_transport import (
    SteamIncomingOfferAcceptTransport,
)


OUR_STEAM_ID = "76561198000000001"
SELLER_STEAM_ID = "76561198000000002"
OFFER_ID = "1234567890"
COOKIE = (
    "sessionid=session-value; steamCountry=TW; "
    f"steamLoginSecure={OUR_STEAM_ID}%7C%7Caccess-token"
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200):
        self.status_code = status_code
        self.payload = payload


class FakeSession:
    verify = True

    def __init__(self, result):
        self.result = result
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("GET must never be invoked")


def _transport(session, *, cookie_string=COOKIE):
    return SteamIncomingOfferAcceptTransport(
        cookie_string,
        session=session,
    )


def _accept(transport, **changes):
    values = {
        "steam_tradeoffer_id": OFFER_ID,
        "account_steam_id": OUR_STEAM_ID,
        "counterparty_steam_id": SELLER_STEAM_ID,
        "timeout_seconds": 5,
    }
    values.update(changes)
    return transport.accept(**values)


def test_exact_valid_input_attempts_one_exact_post_then_result_unknown():
    session = FakeSession(
        FakeResponse({"success": True, "accepted": True}, status_code=200)
    )
    transport = _transport(session)

    with pytest.raises(
        AcceptOfferWriteResultUnknown,
        match="steam_accept_write_result_unknown",
    ):
        _accept(transport)

    assert transport.bound_account_steam_id == OUR_STEAM_ID
    assert session.get_calls == []
    assert session.post_calls == [
        (
            f"https://steamcommunity.com/tradeoffer/{OFFER_ID}/accept",
            {
                "data": {
                    "sessionid": "session-value",
                    "tradeofferid": OFFER_ID,
                    "serverid": "1",
                    "partner": SELLER_STEAM_ID,
                    "captcha": "",
                },
                "headers": {
                    "Referer": (
                        f"https://steamcommunity.com/tradeoffer/{OFFER_ID}"
                    ),
                    "Origin": "https://steamcommunity.com",
                },
                "cookies": {
                    "sessionid": "session-value",
                    "steamCountry": "TW",
                    "steamLoginSecure": (
                        f"{OUR_STEAM_ID}%7C%7Caccess-token"
                    ),
                },
                "timeout": (5.0, 5.0),
                "allow_redirects": False,
            },
        )
    ]


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"success": True}, status_code=200),
        FakeResponse({}, status_code=204),
        FakeResponse(None, status_code=302),
        FakeResponse({"error": "forbidden"}, status_code=403),
        FakeResponse({"error": "server"}, status_code=500),
        {"accepted": True},
        None,
        object(),
    ],
)
def test_every_returned_post_response_is_result_unknown_without_retry(response):
    session = FakeSession(response)

    with pytest.raises(AcceptOfferWriteResultUnknown):
        _accept(_transport(session))

    assert len(session.post_calls) == 1
    assert session.get_calls == []


def test_needs_mobile_confirmation_is_unknown_with_zero_second_request():
    session = FakeSession(
        FakeResponse(
            {"success": True, "needs_mobile_confirmation": True},
            status_code=200,
        )
    )

    with pytest.raises(AcceptOfferWriteResultUnknown):
        _accept(_transport(session))

    assert len(session.post_calls) == 1
    assert session.get_calls == []


@pytest.mark.parametrize(
    "error",
    [
        requests.Timeout("timeout"),
        requests.ConnectionError("connection reset"),
        RuntimeError("arbitrary post exception"),
    ],
)
def test_any_post_exception_is_result_unknown_without_retry(error):
    session = FakeSession(error)

    with pytest.raises(AcceptOfferWriteResultUnknown) as caught:
        _accept(_transport(session))

    assert str(error) not in str(caught.value)
    assert len(session.post_calls) == 1
    assert session.get_calls == []


@pytest.mark.parametrize(
    "account_steam_id",
    ["76561198000000003", 76561198000000001, True],
)
def test_wrong_or_noncanonical_account_is_preflight_failure(account_steam_id):
    session = FakeSession(FakeResponse())

    with pytest.raises(AcceptOfferPreflightError):
        _accept(_transport(session), account_steam_id=account_steam_id)

    assert session.post_calls == []
    assert session.get_calls == []


@pytest.mark.parametrize(
    "offer_id",
    [
        "",
        " 123",
        "123 ",
        "0123",
        123,
        True,
        123.0,
        [],
        {},
        "not-decimal",
    ],
)
def test_malformed_offer_id_is_rejected_before_post(offer_id):
    session = FakeSession(FakeResponse())

    with pytest.raises(AcceptOfferPreflightError):
        _accept(_transport(session), steam_tradeoffer_id=offer_id)

    assert session.post_calls == []
    assert session.get_calls == []


@pytest.mark.parametrize(
    "counterparty_steam_id",
    ["", " 123", "123 ", "0123", 123, True, 123.0, [], {}, "seller"],
)
def test_malformed_counterparty_is_rejected_before_post(counterparty_steam_id):
    session = FakeSession(FakeResponse())

    with pytest.raises(AcceptOfferPreflightError):
        _accept(
            _transport(session),
            counterparty_steam_id=counterparty_steam_id,
        )

    assert session.post_calls == []
    assert session.get_calls == []


def test_self_counterparty_is_rejected_before_post():
    session = FakeSession(FakeResponse())

    with pytest.raises(AcceptOfferPreflightError):
        _accept(_transport(session), counterparty_steam_id=OUR_STEAM_ID)

    assert session.post_calls == []
    assert session.get_calls == []


@pytest.mark.parametrize(
    "cookie_string",
    [
        None,
        "",
        " ",
        "sessionid=session-value",
        f"steamLoginSecure={OUR_STEAM_ID}||token",
        f"steamLoginSecure={OUR_STEAM_ID}; sessionid=session-value",
        "steamLoginSecure=malformed; sessionid=session-value",
        f"steamLoginSecure=0{OUR_STEAM_ID}||token; sessionid=session-value",
        f"steamLoginSecure={OUR_STEAM_ID}||; sessionid=session-value",
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token||extra; "
            "sessionid=session-value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            f"steamLoginSecure={OUR_STEAM_ID}||other; "
            "sessionid=session-value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            "sessionid="
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            "sessionid=one; sessionid=two"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            "sessionid =session-value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            "sessionid=session value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; "
            "sessionid=session-value "
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID} ||token; "
            "sessionid=session-value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token; broken; "
            "sessionid=session-value"
        ),
        (
            f"steamLoginSecure={OUR_STEAM_ID}||token;; "
            "sessionid=session-value"
        ),
    ],
)
def test_missing_malformed_or_duplicate_cookies_are_rejected(cookie_string):
    session = FakeSession(FakeResponse())

    with pytest.raises(PlatformAdapterProtocolError):
        _transport(session, cookie_string=cookie_string)

    assert session.post_calls == []
    assert session.get_calls == []


def test_raw_secure_cookie_separator_is_accepted_and_binds_exact_identity():
    session = FakeSession(FakeResponse())
    cookie = (
        f"steamLoginSecure={OUR_STEAM_ID}||access-token; "
        "sessionid=session-value"
    )

    transport = _transport(session, cookie_string=cookie)

    assert transport.bound_account_steam_id == OUR_STEAM_ID
    with pytest.raises(AttributeError):
        transport.bound_account_steam_id = SELLER_STEAM_ID
    assert session.post_calls == []


def test_explicit_tls_disable_is_rejected_before_post():
    session = FakeSession(FakeResponse())
    session.verify = False

    with pytest.raises(
        PlatformAdapterProtocolError,
        match="TLS verification must remain enabled",
    ):
        _transport(session)

    assert session.post_calls == []


def test_tls_disable_after_construction_is_rejected_before_post():
    session = FakeSession(FakeResponse())
    transport = _transport(session)
    session.verify = False

    with pytest.raises(
        AcceptOfferPreflightError,
        match="TLS verification must remain enabled",
    ):
        _accept(transport)

    assert session.post_calls == []
    assert session.get_calls == []


@pytest.mark.parametrize("post", [None, "not-callable", 1])
def test_missing_or_noncallable_post_is_rejected(post):
    class InvalidSession:
        verify = True

    session = InvalidSession()
    if post is not None:
        session.post = post

    with pytest.raises(
        PlatformAdapterProtocolError,
        match="session must provide POST",
    ):
        _transport(session)


@pytest.mark.parametrize(
    "timeout_seconds",
    [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "5", [], {}],
)
def test_invalid_timeout_is_rejected_before_post(timeout_seconds):
    session = FakeSession(FakeResponse())

    with pytest.raises(AcceptOfferPreflightError):
        _accept(_transport(session), timeout_seconds=timeout_seconds)

    assert session.post_calls == []
    assert session.get_calls == []
