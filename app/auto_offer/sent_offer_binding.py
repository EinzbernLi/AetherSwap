"""Pure contract for normalized buyer sent-offer discovery and exact closure.

This module deliberately knows nothing about Steam wire fields, timestamps,
HTTP, authentication, retries, persistence, or worker scheduling.  A future
transport may supply an already-normalized candidate set only after its real
schema and time semantics are separately verified.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.auto_offer.adapters import SteamTradeOfferEvidence


class SentOfferBindingContractError(ValueError):
    """Raised when normalized discovery or exact closure is unsafe."""


class SentOfferBindingAmbiguousError(SentOfferBindingContractError):
    """Raised when more than one sent-offer candidate remains eligible."""


def _require_id(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise SentOfferBindingContractError(
            f"{field} must be a non-whitespace string"
        )
    return value


def _require_canonical_steam_id(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise SentOfferBindingContractError(
            f"{field} must be a canonical positive SteamID"
        )
    number = int(value)
    if number <= 0 or str(number) != value:
        raise SentOfferBindingContractError(
            f"{field} must be a canonical positive SteamID"
        )
    return value


@dataclass(frozen=True, slots=True)
class SentOfferDiscoveryQuery:
    """Exact local delivery identity anchoring one future discovery read."""

    purchase_id: str
    buff_order_id: str
    account_id: str
    recipient_steam_id: str
    revision: int
    offer_attempted_at: float

    def __post_init__(self) -> None:
        for field in ("purchase_id", "buff_order_id", "account_id"):
            _require_id(getattr(self, field), field)
        _require_canonical_steam_id(
            self.recipient_steam_id,
            "recipient_steam_id",
        )
        if type(self.revision) is not int or self.revision < 1:
            raise SentOfferBindingContractError(
                "revision must be an integer of at least one"
            )
        if (
            type(self.offer_attempted_at) not in (int, float)
            or not math.isfinite(self.offer_attempted_at)
            or self.offer_attempted_at < 0
        ):
            raise SentOfferBindingContractError(
                "offer_attempted_at must be finite and non-negative"
            )
        object.__setattr__(self, "offer_attempted_at", float(self.offer_attempted_at))


@dataclass(frozen=True, slots=True)
class SentOfferDiscoveryEvidence:
    """Candidate IDs already normalized by a future verified discovery provider.

    Candidate order is explicitly non-authoritative.  Canonical sorting prevents
    callers from treating provider order as "latest" or otherwise meaningful.
    """

    query: SentOfferDiscoveryQuery
    candidate_tradeoffer_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.query) is not SentOfferDiscoveryQuery:
            raise SentOfferBindingContractError(
                "query must be a SentOfferDiscoveryQuery"
            )
        SentOfferDiscoveryQuery.__post_init__(self.query)
        if type(self.candidate_tradeoffer_ids) is not tuple:
            raise SentOfferBindingContractError(
                "candidate_tradeoffer_ids must be a tuple"
            )
        canonical: list[str] = []
        for value in self.candidate_tradeoffer_ids:
            canonical.append(_require_id(value, "candidate_tradeoffer_id"))
        if len(set(canonical)) != len(canonical):
            raise SentOfferBindingContractError(
                "candidate_tradeoffer_ids must not contain duplicates"
            )
        object.__setattr__(self, "candidate_tradeoffer_ids", tuple(sorted(canonical)))


@dataclass(frozen=True, slots=True)
class SentOfferBindingEvidence:
    """Minimal immutable identity proven by discovery plus exact Steam read."""

    query: SentOfferDiscoveryQuery
    steam_tradeoffer_id: str
    counterparty_steam_id: str

    def __post_init__(self) -> None:
        if type(self.query) is not SentOfferDiscoveryQuery:
            raise SentOfferBindingContractError(
                "query must be a SentOfferDiscoveryQuery"
            )
        SentOfferDiscoveryQuery.__post_init__(self.query)
        _require_id(self.steam_tradeoffer_id, "steam_tradeoffer_id")
        _require_canonical_steam_id(
            self.counterparty_steam_id,
            "counterparty_steam_id",
        )
        if self.counterparty_steam_id == self.query.recipient_steam_id:
            raise SentOfferBindingContractError(
                "counterparty must differ from recipient"
            )


def select_unique_sent_offer_candidate(
    discovery: SentOfferDiscoveryEvidence,
) -> str | None:
    """Return None for zero, one exact ID for one, and fail closed for two-plus."""

    if type(discovery) is not SentOfferDiscoveryEvidence:
        raise SentOfferBindingContractError(
            "discovery must be SentOfferDiscoveryEvidence"
        )
    SentOfferDiscoveryEvidence.__post_init__(discovery)
    candidates = discovery.candidate_tradeoffer_ids
    if not candidates:
        return None
    if len(candidates) != 1:
        raise SentOfferBindingAmbiguousError("ambiguous_sent_offer_candidates")
    return candidates[0]


def close_exact_sent_offer_candidate(
    discovery: SentOfferDiscoveryEvidence,
    exact_offer: SteamTradeOfferEvidence,
) -> SentOfferBindingEvidence:
    """Close one unique candidate against existing exact Steam offer evidence."""

    candidate = select_unique_sent_offer_candidate(discovery)
    if candidate is None:
        raise SentOfferBindingContractError("sent_offer_candidate_not_found")
    if type(exact_offer) is not SteamTradeOfferEvidence:
        raise SentOfferBindingContractError(
            "exact_offer must be SteamTradeOfferEvidence"
        )
    try:
        SteamTradeOfferEvidence.__post_init__(exact_offer)
    except Exception as exc:
        raise SentOfferBindingContractError("exact_offer_is_malformed") from exc

    query = discovery.query
    if exact_offer.steam_tradeoffer_id != candidate:
        raise SentOfferBindingContractError("tradeoffer_identity_mismatch")
    if exact_offer.account_steam_id != query.recipient_steam_id:
        raise SentOfferBindingContractError("account_identity_mismatch")
    if exact_offer.is_our_offer is not True:
        raise SentOfferBindingContractError("buyer_offer_direction_mismatch")
    try:
        counterparty = _require_canonical_steam_id(
            exact_offer.counterparty_steam_id,
            "counterparty_steam_id",
        )
    except SentOfferBindingContractError as exc:
        raise SentOfferBindingContractError("counterparty_identity_mismatch") from exc
    if counterparty == query.recipient_steam_id:
        raise SentOfferBindingContractError("counterparty_identity_mismatch")
    if exact_offer.items_to_give:
        raise SentOfferBindingContractError("buyer_offer_gives_items")
    # SteamTradeOfferEvidence requires at least one non-empty item side, so once
    # buyer-send is proven to give nothing, at least one received item is implied.

    return SentOfferBindingEvidence(
        query=query,
        steam_tradeoffer_id=candidate,
        counterparty_steam_id=counterparty,
    )


__all__ = [
    "SentOfferBindingAmbiguousError",
    "SentOfferBindingContractError",
    "SentOfferBindingEvidence",
    "SentOfferDiscoveryEvidence",
    "SentOfferDiscoveryQuery",
    "close_exact_sent_offer_candidate",
    "select_unique_sent_offer_candidate",
]
