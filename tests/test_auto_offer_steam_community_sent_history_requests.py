from types import SimpleNamespace

import pytest

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history_requests import (
    CommunitySentHistoryRequestsError,
    RequestsCommunitySentHistoryOneShotSender,
)
from app.auto_offer.steam_community_sent_history_transport import (
    CommunitySentHistoryDisposition,
    CommunitySentHistoryHttpResponse,
    build_community_sent_history_request,
    read_community_sent_history_once,
)


RECIPIENT = "76561198000000001"
COOKIE = "sessionid=session-value; steamLoginSecure=secure-value; browserid=browser-value"


def query():
    return SentOfferDiscoveryQuery(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id=RECIPIENT,
        revision=7,
        offer_attempted_at=1234.5,
    )


class RecorderMap(dict):
    def update(self, values):
        super().update(values)


class FakeSession:
    def __init__(self, *, response=None, error=None):
        self.trust_env = True
        self.verify = False
        self.cookies = RecorderMap()
        self.headers = RecorderMap()
        self.calls = []
        self.closed = False
        self._response = response or SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "text/html; charset=UTF-8"},
            text=(
                '<div class="tradeoffer" id="tradeofferid_100">'
                '<div class="tradeoffer_items_ctn inactive">'
                '<div class="tradeoffer_items_banner accepted"></div>'
                "</div></div>"
            ),
        )
        self._error = error

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        self.closed = True


def test_constructor_injects_cookies_and_hardens_session_without_any_request():
    session = FakeSession()

    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )

    assert session.calls == []
    assert session.trust_env is False
    assert session.verify is True
    assert session.cookies == {
        "sessionid": "session-value",
        "steamLoginSecure": "secure-value",
        "browserid": "browser-value",
    }
    assert session.headers["Accept"] == "text/html,application/xhtml+xml"
    assert session.headers["Referer"] == "https://steamcommunity.com/"
    assert "Mozilla/5.0" in session.headers["User-Agent"]
    sender.close()
    assert session.closed is True


@pytest.mark.parametrize(
    "cookies,expected",
    [
        ("", "cookie_header_required"),
        ("sessionid=x", "steam_login_secure_required"),
        ("steamLoginSecure=", "steam_login_secure_required"),
        ("steamLoginSecure", "malformed_cookie_part"),
        ("bad name=x; steamLoginSecure=y", "malformed_cookie_name"),
        (" steamLoginSecure=x; steamLoginSecure=y", "duplicate_cookie_name"),
        ("steamLoginSecure=x; steamLoginSecure=y", "duplicate_cookie_name"),
        ("steamLoginSecure=x ", "noncanonical_cookie_value"),
    ],
)
def test_malformed_or_missing_auth_cookie_fails_before_session_construction(cookies, expected):
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return FakeSession()

    with pytest.raises(CommunitySentHistoryRequestsError, match=expected):
        RequestsCommunitySentHistoryOneShotSender(
            cookies,
            session_factory=factory,
        )

    assert factory_calls == []


def test_cookie_values_are_preserved_exactly_not_normalized():
    session = FakeSession()
    sender = RequestsCommunitySentHistoryOneShotSender(
        "steamLoginSecure=a%2Fb%2Bc%3D; sessionid=ABC_def-123",
        session_factory=lambda: session,
    )

    assert session.cookies["steamLoginSecure"] == "a%2Fb%2Bc%3D"
    assert session.cookies["sessionid"] == "ABC_def-123"
    sender.close()


def test_exact_d2_plan_becomes_exact_single_get_with_tls_and_no_redirect():
    session = FakeSession()
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )
    plan = build_community_sent_history_request(query())

    result = sender(plan)

    assert isinstance(result, CommunitySentHistoryHttpResponse)
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == (
        "https://steamcommunity.com/profiles/"
        f"{RECIPIENT}/tradeoffers/sent/?history=1"
    )
    assert kwargs == {
        "timeout": (5.0, 15.0),
        "allow_redirects": False,
        "verify": True,
    }


def test_sender_is_single_use_and_never_issues_a_second_get():
    session = FakeSession()
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )
    plan = build_community_sent_history_request(query())

    sender(plan)
    with pytest.raises(CommunitySentHistoryRequestsError, match="sender_already_used"):
        sender(plan)

    assert len(session.calls) == 1


def test_d2_integration_proves_identity_surface_from_fake_html():
    session = FakeSession()
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )

    result = read_community_sent_history_once(query(), sender)

    assert len(session.calls) == 1
    assert result.snapshot.tradeoffer_ids == ("100",)
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
    )
    assert result.outcome.canonical_count == 1
    assert result.outcome.unique_count == 1
    assert not hasattr(result.outcome, "body")
    assert not hasattr(result.outcome, "headers")
    assert not hasattr(result.outcome, "cookies")


def test_request_exception_is_sanitized_by_d2_and_not_retried():
    session = FakeSession(error=RuntimeError("secret secure-value must not surface"))
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )

    result = read_community_sent_history_once(query(), sender)

    assert len(session.calls) == 1
    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.status_class == "none"
    assert not hasattr(result.outcome, "error")
    assert not hasattr(result.outcome, "detail")


def test_response_content_type_and_body_are_only_transient_d2_inputs():
    session = FakeSession(
        response=SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"secret":"do-not-parse"}',
        )
    )
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )

    result = read_community_sent_history_once(query(), sender)

    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.NON_HTML_REJECTED
    assert not hasattr(result.outcome, "body")


def test_invalid_session_factory_contract_fails_closed():
    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="session_factory_must_be_callable",
    ):
        RequestsCommunitySentHistoryOneShotSender(COOKIE, session_factory=None)

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="session_factory_returned_none",
    ):
        RequestsCommunitySentHistoryOneShotSender(
            COOKIE,
            session_factory=lambda: None,
        )


def test_bad_response_shape_propagates_to_d2_as_sanitized_transport_error():
    session = FakeSession(
        response=SimpleNamespace(
            status_code="200",
            headers={"Content-Type": "text/html"},
            text="<html></html>",
        )
    )
    sender = RequestsCommunitySentHistoryOneShotSender(
        COOKIE,
        session_factory=lambda: session,
    )

    result = read_community_sent_history_once(query(), sender)

    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert len(session.calls) == 1
