import ast
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.auto_offer.sent_offer_binding import (
    SentOfferBindingAmbiguousError,
    SentOfferDiscoveryQuery,
    select_unique_sent_offer_candidate,
)
from app.auto_offer.steam_community_sent_history import (
    CommunitySentOfferSnapshot,
    SteamCommunitySentHistoryError,
    discover_sent_offer_delta,
    parse_community_sent_history_html,
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


def offer_html(
    tradeoffer_id: str,
    *,
    accepted: bool = True,
    inactive: bool = True,
    classinfo: bool = True,
) -> str:
    item_attr = (
        ' data-economy-item="classinfo/730/2/123456"' if classinfo else ""
    )
    inactive_class = " inactive" if inactive else ""
    accepted_banner = (
        '<div class="tradeoffer_items_banner accepted"></div>' if accepted else ""
    )
    return f"""
    <div class="tradeoffer" id="tradeofferid_{tradeoffer_id}">
      <div class="tradeoffer_items_ctn{inactive_class}">
        {accepted_banner}
        <div class="tradeoffer_item_list">
          <div class="trade_item"{item_attr}></div>
        </div>
      </div>
    </div>
    """


def test_module_is_pure_stdlib_parser_without_transport_or_runtime_dependencies():
    module = importlib.import_module("app.auto_offer.steam_community_sent_history")
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
            "bs4",
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
    assert "AutoOfferStore" not in source
    assert "steamLoginSecure" not in source
    assert "access_token" not in source
    assert "sleep(" not in source


def test_parser_extracts_only_canonical_offer_ids_and_ignores_lifecycle_and_classinfo():
    html = (
        offer_html("1003", accepted=True, inactive=True, classinfo=True)
        + offer_html("1001", accepted=False, inactive=True, classinfo=True)
        + offer_html("1002", accepted=True, inactive=False, classinfo=False)
    )

    snapshot = parse_community_sent_history_html(html)

    assert snapshot.tradeoffer_ids == ("1001", "1002", "1003")
    assert not hasattr(snapshot, "accepted")
    assert not hasattr(snapshot, "inactive")
    assert not hasattr(snapshot, "items")
    with pytest.raises(FrozenInstanceError):
        snapshot.tradeoffer_ids = ()


def test_non_tradeoffer_element_with_tradeofferid_shape_is_not_authoritative():
    snapshot = parse_community_sent_history_html(
        '<div class="other" id="tradeofferid_123"></div>'
    )

    assert snapshot.tradeoffer_ids == ()


def test_empty_history_is_structurally_valid_for_pure_parser():
    assert parse_community_sent_history_html("<html><body></body></html>") == (
        CommunitySentOfferSnapshot(())
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        ('<div class="tradeoffer"></div>', "tradeoffer_node_missing_id"),
        (
            '<div class="tradeoffer" id="offer_123"></div>',
            "malformed_tradeoffer_element_id",
        ),
        (
            '<div class="tradeoffer" id="tradeofferid_0123"></div>',
            "malformed_tradeoffer_element_id",
        ),
        (
            '<div class="tradeoffer" id="tradeofferid_0"></div>',
            "malformed_tradeoffer_element_id",
        ),
    ],
)
def test_parser_fails_closed_on_tradeoffer_nodes_without_canonical_identity(html, expected):
    with pytest.raises(SteamCommunitySentHistoryError, match=expected):
        parse_community_sent_history_html(html)


def test_parser_fails_closed_on_duplicate_canonical_offer_id():
    html = offer_html("123") + offer_html("123")

    with pytest.raises(SteamCommunitySentHistoryError, match="duplicate_tradeoffer_id"):
        parse_community_sent_history_html(html)


def test_parser_rejects_non_string_input():
    with pytest.raises(SteamCommunitySentHistoryError, match="html_must_be_string"):
        parse_community_sent_history_html(b"<html></html>")


@pytest.mark.parametrize(
    "ids,expected",
    [
        (["1"], "tradeoffer_ids_must_be_tuple"),
        (("",), "noncanonical_tradeoffer_id"),
        (("01",), "noncanonical_tradeoffer_id"),
        (("0",), "noncanonical_tradeoffer_id"),
        (("abc",), "noncanonical_tradeoffer_id"),
        (("1", "1"), "duplicate_tradeoffer_id"),
    ],
)
def test_snapshot_rejects_noncanonical_or_duplicate_identity(ids, expected):
    with pytest.raises(SteamCommunitySentHistoryError, match=expected):
        CommunitySentOfferSnapshot(ids)


def test_snapshot_order_is_non_authoritative_and_numeric_canonical():
    snapshot = CommunitySentOfferSnapshot(("20", "3", "11"))

    assert snapshot.tradeoffer_ids == ("3", "11", "20")


def test_delta_is_post_minus_pre_and_does_not_use_latest_or_time_matching():
    before = CommunitySentOfferSnapshot(("100", "200", "300"))
    after = CommunitySentOfferSnapshot(("100", "200", "300", "400"))

    discovery = discover_sent_offer_delta(query(), before, after)

    assert discovery.query == query()
    assert discovery.candidate_tradeoffer_ids == ("400",)
    assert select_unique_sent_offer_candidate(discovery) == "400"


def test_delta_allows_old_history_entries_to_disappear_without_inventing_identity():
    before = CommunitySentOfferSnapshot(("100", "200", "300"))
    after = CommunitySentOfferSnapshot(("200", "300", "400"))

    discovery = discover_sent_offer_delta(query(), before, after)

    assert discovery.candidate_tradeoffer_ids == ("400",)


def test_delta_zero_candidate_waits_without_resend_identity():
    before = CommunitySentOfferSnapshot(("100", "200"))
    after = CommunitySentOfferSnapshot(("200", "100"))

    discovery = discover_sent_offer_delta(query(), before, after)

    assert discovery.candidate_tradeoffer_ids == ()
    assert select_unique_sent_offer_candidate(discovery) is None


def test_delta_two_new_candidates_reuses_existing_fail_closed_ambiguity_contract():
    before = CommunitySentOfferSnapshot(("100",))
    after = CommunitySentOfferSnapshot(("100", "201", "200"))

    discovery = discover_sent_offer_delta(query(), before, after)

    assert discovery.candidate_tradeoffer_ids == ("200", "201")
    with pytest.raises(
        SentOfferBindingAmbiguousError,
        match="ambiguous_sent_offer_candidates",
    ):
        select_unique_sent_offer_candidate(discovery)


def test_delta_requires_exact_contract_types():
    before = CommunitySentOfferSnapshot(("100",))
    after = CommunitySentOfferSnapshot(("100", "200"))

    with pytest.raises(
        SteamCommunitySentHistoryError,
        match="query_must_be_sent_offer_discovery_query",
    ):
        discover_sent_offer_delta(object(), before, after)
    with pytest.raises(
        SteamCommunitySentHistoryError,
        match="before_must_be_snapshot",
    ):
        discover_sent_offer_delta(query(), object(), after)
    with pytest.raises(
        SteamCommunitySentHistoryError,
        match="after_must_be_snapshot",
    ):
        discover_sent_offer_delta(query(), before, object())
