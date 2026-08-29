"""Pure Steam Community sent-history identity parsing and snapshot delta.

The Community HTML surface is intentionally used for one narrow authority only:
canonical sent Trade Offer IDs.  Lifecycle classes, item metadata, ordering,
timestamps, and partner/item presentation are non-authoritative here and are
left to the existing exact ``GetTradeOffer`` closure.

This module performs no HTTP, authentication, persistence, retry, scheduling,
or Store/Host work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from app.auto_offer.sent_offer_binding import (
    SentOfferBindingContractError,
    SentOfferDiscoveryEvidence,
    SentOfferDiscoveryQuery,
)


_CANONICAL_TRADEOFFER_ELEMENT_ID = re.compile(r"^tradeofferid_([1-9][0-9]*)$")


class SteamCommunitySentHistoryError(SentOfferBindingContractError):
    """Raised when Community sent-history identity evidence is unsafe."""


def _canonical_tradeoffer_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise SteamCommunitySentHistoryError("noncanonical_tradeoffer_id")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise SteamCommunitySentHistoryError("noncanonical_tradeoffer_id")
    return value


@dataclass(frozen=True, slots=True)
class CommunitySentOfferSnapshot:
    """Canonical sent Trade Offer IDs from one authenticated Community read.

    Ordering is deliberately discarded.  A snapshot is identity evidence only;
    it does not imply lifecycle, recency, item identity, or completeness beyond
    the separately verified transport/page contract.
    """

    tradeoffer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.tradeoffer_ids) is not tuple:
            raise SteamCommunitySentHistoryError("tradeoffer_ids_must_be_tuple")
        canonical = tuple(_canonical_tradeoffer_id(v) for v in self.tradeoffer_ids)
        if len(set(canonical)) != len(canonical):
            raise SteamCommunitySentHistoryError("duplicate_tradeoffer_id")
        object.__setattr__(self, "tradeoffer_ids", tuple(sorted(canonical, key=int)))


class _SentHistoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tradeoffer_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        attr_map = {name: value for name, value in attrs}
        classes = set((attr_map.get("class") or "").split())
        if "tradeoffer" not in classes:
            return

        element_id = attr_map.get("id")
        if type(element_id) is not str:
            raise SteamCommunitySentHistoryError("tradeoffer_node_missing_id")
        match = _CANONICAL_TRADEOFFER_ELEMENT_ID.fullmatch(element_id)
        if match is None:
            raise SteamCommunitySentHistoryError("malformed_tradeoffer_element_id")
        self.tradeoffer_ids.append(_canonical_tradeoffer_id(match.group(1)))


def parse_community_sent_history_html(html: str) -> CommunitySentOfferSnapshot:
    """Parse only canonical sent Trade Offer IDs from server-rendered HTML.

    Empty history is structurally valid at this pure-parser layer.  Proving an
    authenticated sent-history page rather than a login/error page belongs to
    the later transport classifier and must not be guessed from an empty set.
    """

    if type(html) is not str:
        raise SteamCommunitySentHistoryError("html_must_be_string")

    parser = _SentHistoryParser()
    try:
        parser.feed(html)
        parser.close()
    except SteamCommunitySentHistoryError:
        raise
    except Exception as exc:
        raise SteamCommunitySentHistoryError("malformed_sent_history_html") from exc

    return CommunitySentOfferSnapshot(tuple(parser.tradeoffer_ids))


def discover_sent_offer_delta(
    query: SentOfferDiscoveryQuery,
    before: CommunitySentOfferSnapshot,
    after: CommunitySentOfferSnapshot,
) -> SentOfferDiscoveryEvidence:
    """Return deterministic post-minus-pre candidate IDs for existing closure.

    Missing baseline IDs in ``after`` are intentionally ignored: Community
    history may be bounded or paginated, and disappearance of old entries does
    not identify a new offer.  Only IDs newly present after the single-flight
    SEND authority window can become candidates.  Candidate cardinality is then
    handled by the existing 0/1/2+ fail-closed binding contract.
    """

    if type(query) is not SentOfferDiscoveryQuery:
        raise SteamCommunitySentHistoryError("query_must_be_sent_offer_discovery_query")
    SentOfferDiscoveryQuery.__post_init__(query)
    if type(before) is not CommunitySentOfferSnapshot:
        raise SteamCommunitySentHistoryError("before_must_be_snapshot")
    if type(after) is not CommunitySentOfferSnapshot:
        raise SteamCommunitySentHistoryError("after_must_be_snapshot")
    CommunitySentOfferSnapshot.__post_init__(before)
    CommunitySentOfferSnapshot.__post_init__(after)

    before_ids = set(before.tradeoffer_ids)
    candidate_ids = tuple(
        sorted(
            (value for value in after.tradeoffer_ids if value not in before_ids),
            key=int,
        )
    )
    return SentOfferDiscoveryEvidence(
        query=query,
        candidate_tradeoffer_ids=candidate_ids,
    )


__all__ = [
    "CommunitySentOfferSnapshot",
    "SteamCommunitySentHistoryError",
    "discover_sent_offer_delta",
    "parse_community_sent_history_html",
]
