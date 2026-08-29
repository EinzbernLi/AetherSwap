import ast
import importlib
from pathlib import Path

import pytest

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history_transport import (
    CommunitySentHistoryDisposition,
    CommunitySentHistoryHttpResponse,
    CommunitySentHistoryNetworkError,
    CommunitySentHistoryRequestPlan,
    CommunitySentHistoryTransportError,
    build_community_sent_history_request,
    read_community_sent_history_once,
)


RECIPIENT = "76561198000000001"


def query():
    return SentOfferDiscoveryQuery(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id=RECIPIENT,
        revision=7,
        offer_attempted_at=1234.5,
    )


def offer_html(*ids: str) -> str:
    return "".join(
        f'<div class="tradeoffer" id="tradeofferid_{value}">'
        '<div class="tradeoffer_items_ctn inactive">'
        '<div class="tradeoffer_items_banner accepted"></div>'
        '<div class="trade_item" data-economy-item="classinfo/730/2/999"></div>'
        "</div></div>"
        for value in ids
    )


def response(status=200, content_type="text/html; charset=UTF-8", body=""):
    return CommunitySentHistoryHttpResponse(
        status_code=status,
        content_type=content_type,
        body=body,
    )


def test_module_has_no_network_client_runtime_or_secret_reader_imports():
    module = importlib.import_module(
        "app.auto_offer.steam_community_sent_history_transport"
    )
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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

    assert imported.isdisjoint(
        {
            "aiohttp",
            "buff",
            "httpx",
            "requests",
            "socket",
            "sqlite3",
            "steam",
            "threading",
            "time",
        }
    )
    assert "steamLoginSecure" not in source
    assert "access_token" not in source
    assert "get_steam_credentials" not in source
    assert "AutoOfferStore" not in source


def test_request_plan_is_exact_profile_history_get_without_redirect_retry_or_polling():
    plan = build_community_sent_history_request(query())

    assert plan == CommunitySentHistoryRequestPlan(
        method="GET",
        url=(
            "https://steamcommunity.com/profiles/"
            f"{RECIPIENT}/tradeoffers/sent/?history=1"
        ),
        allow_redirects=False,
        timeout=(5.0, 15.0),
        verify_tls=True,
        retry_count=0,
        polling_count=0,
        request_budget=1,
    )
    assert "/my/" not in plan.url
    assert plan.url.count("?") == 1
    assert plan.url.endswith("?history=1")


def test_request_builder_rejects_non_query_input():
    with pytest.raises(
        CommunitySentHistoryTransportError,
        match="query_must_be_sent_offer_discovery_query",
    ):
        build_community_sent_history_request(object())


def test_sender_is_invoked_exactly_once_and_receives_frozen_plan():
    calls = []

    def sender(plan):
        calls.append(plan)
        return response(body=offer_html("100", "101"))

    result = read_community_sent_history_once(query(), sender)

    assert len(calls) == 1
    assert calls[0].request_budget == 1
    assert result.snapshot.tradeoffer_ids == ("100", "101")
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
    )
    assert result.outcome.status_class == "2xx"
    assert result.outcome.canonical_count == 2
    assert result.outcome.unique_count == 2
    assert result.outcome.request_count == 1
    assert result.outcome.identity_surface_proven is True
    assert result.outcome.transport_subtype == "none"


def test_lifecycle_classinfo_and_dom_order_do_not_enter_sanitized_outcome():
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(body=offer_html("12", "3", "8")),
    )

    assert result.snapshot.tradeoffer_ids == ("3", "8", "12")
    assert vars(result.outcome) if hasattr(result.outcome, "__dict__") else True
    assert not hasattr(result.outcome, "accepted")
    assert not hasattr(result.outcome, "inactive")
    assert not hasattr(result.outcome, "items")
    assert not hasattr(result.outcome, "tradeoffer_ids")
    assert not hasattr(result.outcome, "body")
    assert not hasattr(result.outcome, "url")
    assert not hasattr(result.outcome, "exception")
    assert not hasattr(result.outcome, "error_text")


def test_empty_html_identity_set_is_explicitly_unproven_not_authorized_empty_snapshot():
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(body="<html><body></body></html>"),
    )

    assert result.snapshot.tradeoffer_ids == ()
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.EMPTY_IDENTITY_SURFACE_UNPROVEN
    )
    assert result.outcome.identity_surface_proven is False
    assert result.outcome.canonical_count == 0
    assert result.outcome.unique_count == 0
    assert result.outcome.transport_subtype == "none"


@pytest.mark.parametrize("status", [100, 204, 400, 401, 403, 404, 500, 503])
def test_non_200_status_fails_closed_without_parsing_identity(status):
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(status=status, body=offer_html("100")),
    )

    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.HTTP_REJECTED
    assert result.outcome.status_class == f"{status // 100}xx"
    assert result.outcome.identity_surface_proven is False
    assert result.outcome.transport_subtype == "none"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_response_is_separately_rejected(status):
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(status=status, body=offer_html("100")),
    )

    assert result.snapshot is None
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.REDIRECT_REJECTED
    )
    assert result.outcome.identity_surface_proven is False
    assert result.outcome.transport_subtype == "none"


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "", "image/png"],
)
def test_http_200_non_html_content_type_fails_closed(content_type):
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(
            content_type=content_type,
            body=offer_html("100"),
        ),
    )

    assert result.snapshot is None
    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.NON_HTML_REJECTED
    )
    assert result.outcome.transport_subtype == "none"


@pytest.mark.parametrize("content_type", ["text/html", "application/xhtml+xml"])
def test_supported_html_media_types_reach_d1_parser(content_type):
    result = read_community_sent_history_once(
        query(),
        lambda plan: response(
            content_type=content_type,
            body=offer_html("100"),
        ),
    )

    assert result.outcome.disposition is (
        CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
    )
    assert result.outcome.transport_subtype == "none"


def test_malformed_or_duplicate_identity_evidence_fails_closed_without_raw_detail():
    malformed = read_community_sent_history_once(
        query(),
        lambda plan: response(
            body='<div class="tradeoffer" id="tradeofferid_001"></div>'
        ),
    )
    duplicate = read_community_sent_history_once(
        query(),
        lambda plan: response(body=offer_html("100", "100")),
    )

    for result in (malformed, duplicate):
        assert result.snapshot is None
        assert result.outcome.disposition is (
            CommunitySentHistoryDisposition.MALFORMED_IDENTITY_REJECTED
        )
        assert result.outcome.canonical_count == 0
        assert result.outcome.unique_count == 0
        assert result.outcome.transport_subtype == "none"


@pytest.mark.parametrize("subtype", ["tls", "timeout", "other"])
def test_safe_transport_subtypes_are_preserved_without_exception_text(subtype):
    calls = []

    def sender(plan):
        calls.append(plan)
        raise CommunitySentHistoryNetworkError(subtype)

    result = read_community_sent_history_once(query(), sender)

    assert len(calls) == 1
    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.status_class == "none"
    assert result.outcome.request_count == 1
    assert result.outcome.transport_subtype == subtype
    assert not hasattr(result.outcome, "error")
    assert not hasattr(result.outcome, "detail")
    assert not hasattr(result.outcome, "exception")


def test_unknown_transport_exception_collapses_to_other_and_never_retries():
    calls = []

    def sender(plan):
        calls.append(plan)
        raise RuntimeError(
            "secret-token=do-not-surface https://steamcommunity.com/profiles/123"
        )

    result = read_community_sent_history_once(query(), sender)

    assert len(calls) == 1
    assert result.snapshot is None
    assert result.outcome.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR
    assert result.outcome.status_class == "none"
    assert result.outcome.request_count == 1
    assert result.outcome.transport_subtype == "other"
    assert not hasattr(result.outcome, "error")
    assert not hasattr(result.outcome, "detail")


def test_invalid_transport_subtype_is_rejected():
    with pytest.raises(CommunitySentHistoryTransportError, match="invalid_transport_subtype"):
        CommunitySentHistoryNetworkError("certificate-text")


def test_invalid_sender_return_type_is_contract_error_not_guessed_http_response():
    with pytest.raises(
        CommunitySentHistoryTransportError,
        match="sender_returned_invalid_response",
    ):
        read_community_sent_history_once(query(), lambda plan: object())


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"method": "POST"}, "method_must_be_get"),
        ({"allow_redirects": True}, "redirects_must_be_disabled"),
        ({"timeout": (1.0, 1.0)}, "timeout_contract_mismatch"),
        ({"verify_tls": False}, "tls_verification_required"),
        ({"retry_count": 1}, "retry_must_be_zero"),
        ({"polling_count": 1}, "polling_must_be_zero"),
        ({"request_budget": 2}, "request_budget_must_be_one"),
    ],
)
def test_request_plan_rejects_relaxed_safety_invariants(changes, expected):
    values = dict(
        method="GET",
        url=(
            "https://steamcommunity.com/profiles/"
            f"{RECIPIENT}/tradeoffers/sent/?history=1"
        ),
        allow_redirects=False,
        timeout=(5.0, 15.0),
        verify_tls=True,
        retry_count=0,
        polling_count=0,
        request_budget=1,
    )
    values.update(changes)

    with pytest.raises(CommunitySentHistoryTransportError, match=expected):
        CommunitySentHistoryRequestPlan(**values)
