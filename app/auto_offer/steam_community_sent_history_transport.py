"""Bounded read contract for Steam Community sent-history HTML.

This module deliberately does not create a network client, read credentials, or
bind runtime configuration. A caller supplies one sender callable. The module
builds the exact GET plan, invokes the sender at most once, classifies a minimal
response view, and hands canonical identity evidence to the D1 parser.

Raw HTML, redirect destinations, exception text, request URLs and secrets never
appear in the sanitized outcome object.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history import (
    CommunitySentOfferSnapshot,
    SteamCommunitySentHistoryError,
    parse_community_sent_history_html,
)


class CommunitySentHistoryTransportError(ValueError):
    """Raised when the offline read contract itself is malformed."""


class CommunitySentHistoryNetworkError(CommunitySentHistoryTransportError):
    """Safe network exception carrying only a bounded non-secret subtype."""

    __slots__ = ("subtype",)

    def __init__(self, subtype: str) -> None:
        if subtype not in {"tls", "timeout", "other"}:
            raise CommunitySentHistoryTransportError("invalid_transport_subtype")
        self.subtype = subtype
        super().__init__(subtype)


class CommunitySentHistoryDisposition(str, Enum):
    IDENTITY_SURFACE_PROVEN = "identity_surface_proven"
    EMPTY_IDENTITY_SURFACE_UNPROVEN = "empty_identity_surface_unproven"
    REDIRECT_REJECTED = "redirect_rejected"
    HTTP_REJECTED = "http_rejected"
    NON_HTML_REJECTED = "non_html_rejected"
    MALFORMED_IDENTITY_REJECTED = "malformed_identity_rejected"
    TRANSPORT_ERROR = "transport_error"


COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES = frozenset(
    {
        "none",
        "missing_or_invalid",
        "same_target",
        "same_profile_tradeoffers",
        "steam_login_or_auth",
        "same_origin_other",
        "cross_origin",
    }
)


@dataclass(frozen=True, slots=True)
class CommunitySentHistoryRequestPlan:
    method: str
    url: str
    allow_redirects: bool
    timeout: tuple[float, float]
    verify_tls: bool
    retry_count: int
    polling_count: int
    request_budget: int

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise CommunitySentHistoryTransportError("method_must_be_get")
        if type(self.url) is not str or not self.url:
            raise CommunitySentHistoryTransportError("url_must_be_nonempty")
        if self.allow_redirects is not False:
            raise CommunitySentHistoryTransportError("redirects_must_be_disabled")
        if self.timeout != (5.0, 15.0):
            raise CommunitySentHistoryTransportError("timeout_contract_mismatch")
        if self.verify_tls is not True:
            raise CommunitySentHistoryTransportError("tls_verification_required")
        if self.retry_count != 0:
            raise CommunitySentHistoryTransportError("retry_must_be_zero")
        if self.polling_count != 0:
            raise CommunitySentHistoryTransportError("polling_must_be_zero")
        if self.request_budget != 1:
            raise CommunitySentHistoryTransportError("request_budget_must_be_one")


@dataclass(frozen=True, slots=True)
class CommunitySentHistoryHttpResponse:
    """Minimal response view supplied by the concrete bounded adapter."""

    status_code: int
    content_type: str
    body: str
    redirect_subtype: str = "none"

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise CommunitySentHistoryTransportError("invalid_http_status")
        if type(self.content_type) is not str:
            raise CommunitySentHistoryTransportError("content_type_must_be_string")
        if type(self.body) is not str:
            raise CommunitySentHistoryTransportError("body_must_be_string")
        if self.redirect_subtype not in COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES:
            raise CommunitySentHistoryTransportError("invalid_redirect_subtype")

        is_redirect = 300 <= self.status_code <= 399
        if is_redirect and self.redirect_subtype == "none":
            raise CommunitySentHistoryTransportError("redirect_response_requires_subtype")
        if not is_redirect and self.redirect_subtype != "none":
            raise CommunitySentHistoryTransportError("non_redirect_response_has_subtype")


@dataclass(frozen=True, slots=True)
class CommunitySentHistorySanitizedOutcome:
    disposition: CommunitySentHistoryDisposition
    status_class: str
    canonical_count: int
    unique_count: int
    request_count: int
    identity_surface_proven: bool
    transport_subtype: str = "none"
    redirect_subtype: str = "none"

    def __post_init__(self) -> None:
        if type(self.disposition) is not CommunitySentHistoryDisposition:
            raise CommunitySentHistoryTransportError("invalid_disposition")
        if self.status_class not in {"none", "1xx", "2xx", "3xx", "4xx", "5xx"}:
            raise CommunitySentHistoryTransportError("invalid_status_class")
        for field in ("canonical_count", "unique_count", "request_count"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise CommunitySentHistoryTransportError(f"invalid_{field}")
        if self.unique_count > self.canonical_count:
            raise CommunitySentHistoryTransportError("unique_count_exceeds_canonical")
        if self.request_count not in (0, 1):
            raise CommunitySentHistoryTransportError("request_count_exceeds_budget")
        if type(self.identity_surface_proven) is not bool:
            raise CommunitySentHistoryTransportError("invalid_identity_surface_proven")
        if self.transport_subtype not in {"none", "tls", "timeout", "other"}:
            raise CommunitySentHistoryTransportError("invalid_transport_subtype")
        if self.redirect_subtype not in COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES:
            raise CommunitySentHistoryTransportError("invalid_redirect_subtype")

        expected = self.disposition is CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
        if self.identity_surface_proven is not expected:
            raise CommunitySentHistoryTransportError("identity_surface_disposition_mismatch")
        if self.disposition is CommunitySentHistoryDisposition.TRANSPORT_ERROR:
            if self.transport_subtype == "none":
                raise CommunitySentHistoryTransportError("transport_error_requires_subtype")
        elif self.transport_subtype != "none":
            raise CommunitySentHistoryTransportError("non_transport_outcome_has_subtype")

        if self.disposition is CommunitySentHistoryDisposition.REDIRECT_REJECTED:
            if self.redirect_subtype == "none":
                raise CommunitySentHistoryTransportError("redirect_outcome_requires_subtype")
        elif self.redirect_subtype != "none":
            raise CommunitySentHistoryTransportError("non_redirect_outcome_has_subtype")


@dataclass(frozen=True, slots=True)
class CommunitySentHistoryReadResult:
    """Internal normalized snapshot plus public-safe outcome metadata."""

    snapshot: CommunitySentOfferSnapshot | None
    outcome: CommunitySentHistorySanitizedOutcome

    def __post_init__(self) -> None:
        if self.snapshot is not None and type(self.snapshot) is not CommunitySentOfferSnapshot:
            raise CommunitySentHistoryTransportError("invalid_snapshot")
        if type(self.outcome) is not CommunitySentHistorySanitizedOutcome:
            raise CommunitySentHistoryTransportError("invalid_outcome")
        CommunitySentHistorySanitizedOutcome.__post_init__(self.outcome)
        if self.outcome.identity_surface_proven:
            if self.snapshot is None or not self.snapshot.tradeoffer_ids:
                raise CommunitySentHistoryTransportError("proven_surface_requires_snapshot")
        if self.snapshot is not None:
            CommunitySentOfferSnapshot.__post_init__(self.snapshot)
            count = len(self.snapshot.tradeoffer_ids)
            if count != self.outcome.canonical_count or count != self.outcome.unique_count:
                raise CommunitySentHistoryTransportError("snapshot_count_mismatch")


CommunitySentHistorySender = Callable[
    [CommunitySentHistoryRequestPlan],
    CommunitySentHistoryHttpResponse,
]


def build_community_sent_history_request(
    query: SentOfferDiscoveryQuery,
) -> CommunitySentHistoryRequestPlan:
    if type(query) is not SentOfferDiscoveryQuery:
        raise CommunitySentHistoryTransportError("query_must_be_sent_offer_discovery_query")
    SentOfferDiscoveryQuery.__post_init__(query)
    steam_id = query.recipient_steam_id
    return CommunitySentHistoryRequestPlan(
        method="GET",
        url=(
            f"https://steamcommunity.com/profiles/{steam_id}/tradeoffers/sent/"
            "?history=1"
        ),
        allow_redirects=False,
        timeout=(5.0, 15.0),
        verify_tls=True,
        retry_count=0,
        polling_count=0,
        request_budget=1,
    )


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _outcome(
    disposition: CommunitySentHistoryDisposition,
    *,
    status_class: str,
    count: int = 0,
    request_count: int = 1,
    transport_subtype: str = "none",
    redirect_subtype: str = "none",
) -> CommunitySentHistorySanitizedOutcome:
    return CommunitySentHistorySanitizedOutcome(
        disposition=disposition,
        status_class=status_class,
        canonical_count=count,
        unique_count=count,
        request_count=request_count,
        identity_surface_proven=(
            disposition is CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
        ),
        transport_subtype=transport_subtype,
        redirect_subtype=redirect_subtype,
    )


def read_community_sent_history_once(
    query: SentOfferDiscoveryQuery,
    sender: CommunitySentHistorySender,
) -> CommunitySentHistoryReadResult:
    """Invoke exactly one injected sender and classify D1 identity evidence.

    Network exceptions are reduced to a bounded subtype. Exception text,
    headers, redirect destinations, URL values, response excerpts and credential
    material are never retained by this contract.
    """

    if not callable(sender):
        raise CommunitySentHistoryTransportError("sender_must_be_callable")
    plan = build_community_sent_history_request(query)

    try:
        response = sender(plan)
    except CommunitySentHistoryNetworkError as exc:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.TRANSPORT_ERROR,
                status_class="none",
                transport_subtype=exc.subtype,
            ),
        )
    except Exception:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.TRANSPORT_ERROR,
                status_class="none",
                transport_subtype="other",
            ),
        )

    if type(response) is not CommunitySentHistoryHttpResponse:
        raise CommunitySentHistoryTransportError("sender_returned_invalid_response")
    CommunitySentHistoryHttpResponse.__post_init__(response)
    status_class = _status_class(response.status_code)

    if 300 <= response.status_code <= 399:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.REDIRECT_REJECTED,
                status_class=status_class,
                redirect_subtype=response.redirect_subtype,
            ),
        )
    if response.status_code != 200:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.HTTP_REJECTED,
                status_class=status_class,
            ),
        )

    media_type = response.content_type.split(";", 1)[0].strip().lower()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.NON_HTML_REJECTED,
                status_class=status_class,
            ),
        )

    try:
        snapshot = parse_community_sent_history_html(response.body)
    except SteamCommunitySentHistoryError:
        return CommunitySentHistoryReadResult(
            snapshot=None,
            outcome=_outcome(
                CommunitySentHistoryDisposition.MALFORMED_IDENTITY_REJECTED,
                status_class=status_class,
            ),
        )

    count = len(snapshot.tradeoffer_ids)
    disposition = (
        CommunitySentHistoryDisposition.IDENTITY_SURFACE_PROVEN
        if count > 0
        else CommunitySentHistoryDisposition.EMPTY_IDENTITY_SURFACE_UNPROVEN
    )
    return CommunitySentHistoryReadResult(
        snapshot=snapshot,
        outcome=_outcome(
            disposition,
            status_class=status_class,
            count=count,
        ),
    )


__all__ = [
    "COMMUNITY_SENT_HISTORY_REDIRECT_SUBTYPES",
    "CommunitySentHistoryDisposition",
    "CommunitySentHistoryHttpResponse",
    "CommunitySentHistoryNetworkError",
    "CommunitySentHistoryReadResult",
    "CommunitySentHistoryRequestPlan",
    "CommunitySentHistorySanitizedOutcome",
    "CommunitySentHistoryTransportError",
    "build_community_sent_history_request",
    "read_community_sent_history_once",
]
