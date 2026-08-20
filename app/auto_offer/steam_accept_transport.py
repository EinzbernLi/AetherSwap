"""Strict one-POST Steam incoming Trade Offer ACCEPT transport.

The transport performs no discovery, retry, response interpretation, mobile
confirmation, login, credential refresh, polling, or background work.
"""

from __future__ import annotations

import math
import re

from app.auto_offer.adapters import PlatformAdapterProtocolError
from app.auto_offer.platform_accept import (
    AcceptOfferPreflightError,
    AcceptOfferWriteResultUnknown,
)


_STEAM_COMMUNITY = "https://steamcommunity.com"
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_COOKIE_VALUE_RE = re.compile(
    r"^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$"
)
_ENCODED_SECURE_SEPARATOR_RE = re.compile(r"%7c%7c", re.IGNORECASE)
_CANONICAL_POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")


def _is_canonical_positive_decimal(value: object) -> bool:
    return (
        type(value) is str
        and value.isascii()
        and _CANONICAL_POSITIVE_DECIMAL_RE.fullmatch(value) is not None
    )


def _parse_cookie_string(cookie_string: object) -> dict[str, str]:
    if type(cookie_string) is not str or not cookie_string:
        raise PlatformAdapterProtocolError(
            "cookie_string must be a non-empty string"
        )

    cookies: dict[str, str] = {}
    for index, raw_part in enumerate(cookie_string.split(";")):
        part = raw_part if index == 0 else raw_part.lstrip(" \t")
        if not part or "=" not in part:
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        name, _, value = part.partition("=")
        if (
            _COOKIE_NAME_RE.fullmatch(name) is None
            or _COOKIE_VALUE_RE.fullmatch(value) is None
            or name in cookies
        ):
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        cookies[name] = value

    if "steamLoginSecure" not in cookies:
        raise PlatformAdapterProtocolError(
            "steamLoginSecure cookie is required"
        )
    if "sessionid" not in cookies:
        raise PlatformAdapterProtocolError("sessionid cookie is required")
    return cookies


def _secure_cookie_identity(secure_cookie: str) -> str:
    raw_count = secure_cookie.count("||")
    encoded_matches = list(_ENCODED_SECURE_SEPARATOR_RE.finditer(secure_cookie))
    if raw_count == 1 and not encoded_matches:
        steam_id, access_token = secure_cookie.split("||", 1)
    elif raw_count == 0 and len(encoded_matches) == 1:
        match = encoded_matches[0]
        steam_id = secure_cookie[: match.start()]
        access_token = secure_cookie[match.end() :]
    else:
        raise PlatformAdapterProtocolError(
            "steamLoginSecure cookie is malformed"
        )
    if not _is_canonical_positive_decimal(steam_id) or not access_token:
        raise PlatformAdapterProtocolError(
            "steamLoginSecure cookie is malformed"
        )
    return steam_id


def _accept_identifier(value: object, field: str) -> str:
    if not _is_canonical_positive_decimal(value):
        raise AcceptOfferPreflightError(
            f"{field} must be a canonical positive decimal string"
        )
    return value


def _request_timeout(value: object) -> tuple[float, float]:
    if type(value) not in (int, float):
        raise AcceptOfferPreflightError(
            "timeout_seconds must be a finite positive number"
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise AcceptOfferPreflightError(
            "timeout_seconds must be a finite positive number"
        ) from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise AcceptOfferPreflightError(
            "timeout_seconds must be a finite positive number"
        )
    return normalized, normalized


class SteamIncomingOfferAcceptTransport:
    """Attempt one exact incoming-offer ACCEPT for one bound Steam account."""

    __slots__ = ("_session", "_post", "_cookies", "_bound_account")

    def __init__(self, cookie_string: str, *, session: object) -> None:
        cookies = _parse_cookie_string(cookie_string)
        bound_account = _secure_cookie_identity(cookies["steamLoginSecure"])
        if getattr(session, "verify", None) is False:
            raise PlatformAdapterProtocolError(
                "TLS verification must remain enabled"
            )
        post = getattr(session, "post", None)
        if not callable(post):
            raise PlatformAdapterProtocolError("session must provide POST")

        self._session = session
        self._post = post
        self._cookies = dict(cookies)
        self._bound_account = bound_account

    @property
    def bound_account_steam_id(self) -> str:
        return self._bound_account

    def accept(
        self,
        *,
        steam_tradeoffer_id: str,
        account_steam_id: str,
        counterparty_steam_id: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        offer_id = _accept_identifier(
            steam_tradeoffer_id,
            "steam_tradeoffer_id",
        )
        account_id = _accept_identifier(account_steam_id, "account_steam_id")
        counterparty_id = _accept_identifier(
            counterparty_steam_id,
            "counterparty_steam_id",
        )
        if account_id != self._bound_account:
            raise AcceptOfferPreflightError("bound Steam account mismatch")
        if counterparty_id == account_id:
            raise AcceptOfferPreflightError(
                "account and counterparty Steam IDs must differ"
            )
        timeout = _request_timeout(timeout_seconds)
        if getattr(self._session, "verify", None) is False:
            raise AcceptOfferPreflightError(
                "TLS verification must remain enabled"
            )

        offer_url = f"{_STEAM_COMMUNITY}/tradeoffer/{offer_id}"
        accept_url = f"{offer_url}/accept"
        data = {
            "sessionid": self._cookies["sessionid"],
            "tradeofferid": offer_id,
            "serverid": "1",
            "partner": counterparty_id,
            "captcha": "",
        }
        headers = {
            "Referer": offer_url,
            "Origin": _STEAM_COMMUNITY,
        }
        cookies = dict(self._cookies)

        try:
            self._post(
                accept_url,
                data=data,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                allow_redirects=False,
            )
        except BaseException:
            raise AcceptOfferWriteResultUnknown(
                "steam_accept_write_result_unknown"
            ) from None
        raise AcceptOfferWriteResultUnknown(
            "steam_accept_write_result_unknown"
        )


__all__ = ["SteamIncomingOfferAcceptTransport"]
