"""One-shot requests adapter for Steam Community sent-history reads.

Credential material is injected by the caller and consumed only in memory.  This
module does not know where credentials are stored and performs no logging,
persistence, retry, polling, redirect following, or Store/Host work.
"""

from __future__ import annotations

import re
from typing import Callable

import requests

from app.auto_offer.sent_offer_binding import SentOfferDiscoveryQuery
from app.auto_offer.steam_community_sent_history_transport import (
    CommunitySentHistoryHttpResponse,
    CommunitySentHistoryRequestPlan,
    CommunitySentHistoryTransportError,
    build_community_sent_history_request,
)


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class CommunitySentHistoryRequestsError(CommunitySentHistoryTransportError):
    """Raised when the concrete one-shot sender cannot be constructed safely."""


def _parse_injected_cookies(cookies_raw: object) -> dict[str, str]:
    if type(cookies_raw) is not str or not cookies_raw:
        raise CommunitySentHistoryRequestsError("cookie_header_required")

    parsed: dict[str, str] = {}
    for raw_part in cookies_raw.split(";"):
        # Cookie headers conventionally place optional whitespace after ';'.
        # Remove that structural separator whitespace only; credential values
        # themselves are never stripped, normalized, hashed, or rewritten.
        part = raw_part.lstrip(" \t")
        if not part:
            continue
        if "=" not in part:
            raise CommunitySentHistoryRequestsError("malformed_cookie_part")
        raw_name, _, value = part.partition("=")
        name = raw_name.strip()
        if not name or _COOKIE_NAME_RE.fullmatch(name) is None:
            raise CommunitySentHistoryRequestsError("malformed_cookie_name")
        if raw_name != name:
            raise CommunitySentHistoryRequestsError("noncanonical_cookie_name")
        if value != value.strip():
            raise CommunitySentHistoryRequestsError("noncanonical_cookie_value")
        if name in parsed:
            raise CommunitySentHistoryRequestsError("duplicate_cookie_name")
        parsed[name] = value

    if not parsed.get("steamLoginSecure"):
        raise CommunitySentHistoryRequestsError("steam_login_secure_required")
    return parsed


class RequestsCommunitySentHistoryOneShotSender:
    """Single-use sender bound to one exact discovery query/request plan."""

    __slots__ = ("_expected_plan", "_session", "_used")

    def __init__(
        self,
        query: SentOfferDiscoveryQuery,
        cookies_raw: str,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        if not callable(session_factory):
            raise CommunitySentHistoryRequestsError("session_factory_must_be_callable")
        # Bind the credential-bearing sender to the exact account/profile URL
        # derived from the validated local discovery query before a session can
        # become dispatch-capable.  Hand-crafted alternate URLs are rejected.
        expected_plan = build_community_sent_history_request(query)
        cookies = _parse_injected_cookies(cookies_raw)
        session = session_factory()
        if session is None:
            raise CommunitySentHistoryRequestsError("session_factory_returned_none")

        session.trust_env = False
        session.verify = True
        session.cookies.update(cookies)
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://steamcommunity.com/",
            }
        )
        self._expected_plan = expected_plan
        self._session = session
        self._used = False

    def __call__(
        self,
        plan: CommunitySentHistoryRequestPlan,
    ) -> CommunitySentHistoryHttpResponse:
        if type(plan) is not CommunitySentHistoryRequestPlan:
            raise CommunitySentHistoryRequestsError("invalid_request_plan")
        CommunitySentHistoryRequestPlan.__post_init__(plan)
        if plan != self._expected_plan:
            raise CommunitySentHistoryRequestsError("request_plan_identity_mismatch")
        if self._used:
            raise CommunitySentHistoryRequestsError("sender_already_used")
        self._used = True

        response = self._session.get(
            plan.url,
            timeout=plan.timeout,
            allow_redirects=plan.allow_redirects,
            verify=plan.verify_tls,
        )
        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None)
        text = getattr(response, "text", None)
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise CommunitySentHistoryRequestsError("invalid_response_status")
        if headers is None or not hasattr(headers, "get"):
            raise CommunitySentHistoryRequestsError("invalid_response_headers")
        if type(text) is not str:
            raise CommunitySentHistoryRequestsError("invalid_response_text")

        content_type = headers.get("Content-Type", "")
        if type(content_type) is not str:
            raise CommunitySentHistoryRequestsError("invalid_content_type")
        return CommunitySentHistoryHttpResponse(
            status_code=status_code,
            content_type=content_type,
            body=text,
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


__all__ = [
    "CommunitySentHistoryRequestsError",
    "RequestsCommunitySentHistoryOneShotSender",
]
