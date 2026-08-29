import ssl
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history_requests import (
    CommunitySentHistoryRequestsError,
    RequestsCommunitySentHistoryOneShotSender,
    WindowsTrustAdapter,
)
from app.auto_offer.steam_community_sent_history_transport import (
    CommunitySentHistoryDisposition,
    CommunitySentHistoryHttpResponse,
    build_community_sent_history_request,
    read_community_sent_history_once,
)


RECIPIENT = "76561198000000001"
COOKIE = "sessionid=session-value; steamLoginSecure=secure-value; browserid=browser-value"


def query(*, recipient=RECIPIENT):
    return SentOfferDiscoveryQuery(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id=recipient,
        revision=7,
        offer_attempted_at=1234.5,
    )


def verified_context():
    context = ssl.create_default_context()
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


class RecorderMap(dict):
    def update(self, values):
        super().update(values)


class FakeSession:
    def __init__(self, *, response=None, error=None):
        self.trust_env = True
        self.verify = False
        self.proxies = RecorderMap({"https": "http://must-be-cleared.invalid"})
        self.cookies = RecorderMap()
        self.headers = RecorderMap()
        self.adapters = {}
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

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        self.closed = True


def make_sender(session, *, cookies=COOKIE, q=None, context=None):
    q = q or query()
    context = context or verified_context()
    return RequestsCommunitySentHistoryOneShotSender(
        q,
        cookies,
        session_factory=lambda: session,
        ssl_context_factory=lambda: context,
    )


def test_constructor_injects_cookies_and_hardens_session_without_any_request():
    session = FakeSession()
    context = verified_context()

    sender = make_sender(session, context=context)

    assert session.calls == []
    assert session.trust_env is False
    assert session.verify is True
    assert session.proxies == {}
    assert session.cookies == {
        "sessionid": "session-value",
        "steamLoginSecure": "secure-value",
        "browserid": "browser-value",
    }
    assert session.headers["Accept"] == "text/html,application/xhtml+xml"
    assert session.headers["Referer"] == "https://steamcommunity.com/"
    assert "Mozilla/5.0" in session.headers["User-Agent"]
    assert isinstance(session.adapters["https://"], WindowsTrustAdapter)

    adapter = session.adapters["https://"]
    assert adapter._ssl_context is context
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is context
    assert adapter.poolmanager.connection_pool_kw["cert_reqs"] == ssl.CERT_REQUIRED
    for field in ("total", "connect", "read", "redirect", "status", "other"):
        assert getattr(adapter.max_retries, field) == 0

    sender.close()
    assert session.closed is True


def test_requests_232_pool_key_hook_preserves_exact_context_and_cert_requirement():
    if not hasattr(HTTPAdapter, "build_connection_pool_key_attributes"):
        pytest.skip("Requests version has no per-request pool-key hook")

    context = verified_context()
    adapter = WindowsTrustAdapter(context)
    prepared = requests.Request("GET", "https://example.invalid/").prepare()

    _, pool_kwargs = adapter.build_connection_pool_key_attributes(
        prepared,
        True,
        None,
    )

    assert pool_kwargs["ssl_context"] is context
    assert pool_kwargs["cert_reqs"] == ssl.CERT_REQUIRED


def test_proxy_transport_is_forbidden_even_if_called_directly():
    adapter = WindowsTrustAdapter(verified_context())
    with pytest.raises(CommunitySentHistoryRequestsError, match="proxy_transport_forbidden"):
        adapter.proxy_manager_for("http://proxy.invalid")


@pytest.mark.parametrize(
    "cookies,expected",
    [
        ("", "cookie_header_required"),
        ("sessionid=x", "steam_login_secure_required"),
        ("steamLoginSecure=", "steam_login_secure_required"),
        ("steamLoginSecure", "malformed_cookie_part"),
        ("bad name=x; steamLoginSecure=y", "malformed_cookie_name"),
        ("steamLoginSecure =x", "noncanonical_cookie_name"),
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
            query(),
            cookies,
            session_factory=factory,
            ssl_context_factory=verified_context,
        )

    assert factory_calls == []


def test_cookie_values_are_preserved_exactly_not_normalized():
    session = FakeSession()
    sender = make_sender(
        session,
        cookies="steamLoginSecure=a%2Fb%2Bc%3D; sessionid=ABC_def-123",
    )

    assert session.cookies["steamLoginSecure"] == "a%2Fb%2Bc%3D"
    assert session.cookies["sessionid"] == "ABC_def-123"
    sender.close()


def test_invalid_ssl_context_fails_before_session_construction():
    factory_calls = []
    bad_context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    bad_context.check_hostname = False
    bad_context.verify_mode = ssl.CERT_NONE

    def factory():
        factory_calls.append(True)
        return FakeSession()

    with pytest.raises(CommunitySentHistoryRequestsError, match="cert_required"):
        RequestsCommunitySentHistoryOneShotSender(
            query(),
            COOKIE,
            session_factory=factory,
            ssl_context_factory=lambda: bad_context,
        )

    assert factory_calls == []


def test_exact_d2_plan_becomes_exact_single_get_with_tls_and_no_redirect():
    session = FakeSession()
    q = query()
    sender = make_sender(session, q=q)
    plan = build_community_sent_history_request(q)

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


def test_sender_rejects_arbitrary_host_plan_before_dispatching_cookies():
    session = FakeSession()
    q = query()
    sender = make_sender(session, q=q)
    exact = build_community_sent_history_request(q)
    hostile = replace(exact, url="https://example.com/collect?history=1")

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="request_plan_identity_mismatch",
    ):
        sender(hostile)

    assert session.calls == []


def test_sender_rejects_other_steam_profile_plan_before_dispatch():
    session = FakeSession()
    q = query()
    sender = make_sender(session, q=q)
    other = build_community_sent_history_request(
        query(recipient="76561198000000002")
    )

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="request_plan_identity_mismatch",
    ):
        sender(other)

    assert session.calls == []


def test_sender_is_single_use_and_never_issues_a_second_get():
    session = FakeSession()
    q = query()
    sender = make_sender(session, q=q)
    plan = build_community_sent_history_request(q)

    sender(plan)
    with pytest.raises(CommunitySentHistoryRequestsError, match="sender_already_used"):
        sender(plan)

    assert len(session.calls) == 1


def test_d2_integration_proves_identity_surface_from_fake_html():
    session = FakeSession()
    q = query()
    sender = make_sender(session, q=q)

    result = read_community_sent_history_once(q, sender)

    assert len(session.calls) == 1
    assert result.snapshot.tradeoffer_ids == ("100",)
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
    )
    assert result.outcome.canonical_count == 1
    assert result.outcome.unique_count == 1
    assert result.outcome.transport_subtype == "none"
    assert not hasattr(result.outcome, "body")
    assert not hasattr(result.outcome, "headers")
    assert not hasattr(result.outcome, "cookies")


def test_d2_query_mismatch_is_sanitized_other_without_request():
    session = FakeSession()
    sender = make_sender(session, q=query())
    other_query = query(recipient="76561198000000002")

    result = read_community_sent_history_once(other_query, sender)

    assert session.calls == []
    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.transport_subtype == "other"


@pytest.mark.parametrize(
    "error,expected_subtype",
    [
        (requests.exceptions.SSLError("secret tls text"), "tls"),
        (requests.exceptions.ConnectTimeout("secret timeout text"), "timeout"),
        (requests.exceptions.ConnectionError("secret transport text"), "other"),
        (RuntimeError("secret runtime text"), "other"),
    ],
)
def test_request_exceptions_are_safely_classified_and_never_retried(error, expected_subtype):
    session = FakeSession(error=error)
    q = query()
    sender = make_sender(session, q=q)

    result = read_community_sent_history_once(q, sender)

    assert len(session.calls) == 1
    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.status_class == "none"
    assert result.outcome.transport_subtype == expected_subtype
    assert not hasattr(result.outcome, "error")
    assert not hasattr(result.outcome, "detail")
    assert not hasattr(result.outcome, "exception")


def test_response_content_type_and_body_are_only_transient_d2_inputs():
    session = FakeSession(
        response=SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"secret":"do-not-parse"}',
        )
    )
    q = query()
    sender = make_sender(session, q=q)

    result = read_community_sent_history_once(q, sender)

    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.NON_HTML_REJECTED
    assert result.outcome.transport_subtype == "none"
    assert not hasattr(result.outcome, "body")


def test_invalid_factories_and_session_contract_fail_closed():
    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="session_factory_must_be_callable",
    ):
        RequestsCommunitySentHistoryOneShotSender(
            query(),
            COOKIE,
            session_factory=None,
            ssl_context_factory=verified_context,
        )

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="ssl_context_factory_must_be_callable",
    ):
        RequestsCommunitySentHistoryOneShotSender(
            query(),
            COOKIE,
            ssl_context_factory=None,
        )

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="adapter_factory_must_be_callable",
    ):
        RequestsCommunitySentHistoryOneShotSender(
            query(),
            COOKIE,
            ssl_context_factory=verified_context,
            adapter_factory=None,
        )

    with pytest.raises(
        CommunitySentHistoryRequestsError,
        match="session_factory_returned_none",
    ):
        RequestsCommunitySentHistoryOneShotSender(
            query(),
            COOKIE,
            session_factory=lambda: None,
            ssl_context_factory=verified_context,
        )


def test_bad_response_shape_propagates_to_d2_as_sanitized_other_transport_error():
    session = FakeSession(
        response=SimpleNamespace(
            status_code="200",
            headers={"Content-Type": "text/html"},
            text="<html></html>",
        )
    )
    q = query()
    sender = make_sender(session, q=q)

    result = read_community_sent_history_once(q, sender)

    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.transport_subtype == "other"
    assert len(session.calls) == 1
