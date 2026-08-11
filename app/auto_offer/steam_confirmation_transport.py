"""Strict exact-ID Steam mobile confirmation transport for Auto Offer.

This module owns one bounded confirmation lookup and one exact confirmation
mutation. It performs no login, credential persistence, retry, polling, worker
or background activity.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import struct
import time
from collections.abc import Mapping
from typing import Any

import requests

from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
)

_STEAM_COMMUNITY = "https://steamcommunity.com"
_GETLIST_URL = _STEAM_COMMUNITY + "/mobileconf/getlist"
_DETAILS_URL_PREFIX = _STEAM_COMMUNITY + "/mobileconf/details/"
_ALLOW_URL = _STEAM_COMMUNITY + "/mobileconf/ajaxop"
_DEFAULT_TIMEOUT = (5.0, 15.0)
_DEFAULT_MAX_JSON_BYTES = 1_000_000
_DEFAULT_MAX_CONFIRMATIONS = 128
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENCODED_SECURE_SEPARATOR_RE = re.compile(r"%7c%7c", re.IGNORECASE)
_TRADEOFFER_HTML_ID_RE = re.compile(
    r"""id\s*=\s*["']tradeoffer_([1-9][0-9]*)["']""",
    re.IGNORECASE,
)


class SteamConfirmationTransportError(RuntimeError):
    """Sanitized preflight/read failure before any confirmation mutation."""


class SteamConfirmationTransportAuthError(SteamConfirmationTransportError):
    """Sanitized authentication/session failure before mutation."""


class SteamConfirmationWriteResultUnknown(RuntimeError):
    """The allow mutation may have executed but its outcome is unproven."""


def _canonical_positive_decimal(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise PlatformAdapterProtocolError(
            f"{field} must be a canonical positive decimal string"
        )
    number = int(value)
    if number <= 0 or str(number) != value:
        raise PlatformAdapterProtocolError(
            f"{field} must be a canonical positive decimal string"
        )
    return value


def _timeout_tuple(value: object) -> tuple[float, float]:
    if type(value) is not tuple or len(value) != 2:
        raise PlatformAdapterProtocolError("timeout must be a (connect, read) tuple")
    normalized: list[float] = []
    for part in value:
        if type(part) not in (int, float) or not math.isfinite(part) or part <= 0:
            raise PlatformAdapterProtocolError(
                "timeout values must be finite and positive"
            )
        normalized.append(float(part))
    return normalized[0], normalized[1]


def _request_timeout(value: object) -> tuple[float, float]:
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise PlatformAdapterProtocolError(
            "timeout_seconds must be a finite positive number"
        )
    normalized = float(value)
    return normalized, normalized


def _positive_limit(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlatformAdapterProtocolError(f"{field} must be a positive integer")
    return value


def _parse_cookie_string(cookie_string: object) -> dict[str, str]:
    if type(cookie_string) is not str or not cookie_string.strip():
        raise PlatformAdapterProtocolError("cookie_string must be a non-empty string")
    cookies: dict[str, str] = {}
    for raw_part in cookie_string.split(";"):
        part = raw_part.lstrip()
        if not part:
            continue
        if part != part.rstrip() or "=" not in part:
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        name, _, value = part.partition("=")
        if (
            not name
            or name != name.strip()
            or _COOKIE_NAME_RE.fullmatch(name) is None
            or not value
            or value != value.strip()
            or name in cookies
        ):
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        cookies[name] = value
    if "steamLoginSecure" not in cookies:
        raise PlatformAdapterProtocolError("steamLoginSecure cookie is required")
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
        raise PlatformAdapterProtocolError("steamLoginSecure cookie is malformed")
    steam_id = _canonical_positive_decimal(steam_id, "steamLoginSecure steamid")
    if (
        not access_token
        or access_token.strip() != access_token
        or any(character.isspace() for character in access_token)
    ):
        raise PlatformAdapterProtocolError("steamLoginSecure cookie is malformed")
    return steam_id


def _identity_secret_bytes(identity_secret: object) -> bytes:
    if (
        type(identity_secret) is not str
        or not identity_secret
        or identity_secret.strip() != identity_secret
        or any(character.isspace() for character in identity_secret)
    ):
        raise PlatformAdapterProtocolError("identity_secret is invalid")
    try:
        decoded = base64.b64decode(identity_secret, validate=True)
    except Exception:
        raise PlatformAdapterProtocolError("identity_secret is invalid") from None
    if len(decoded) != 20:
        raise PlatformAdapterProtocolError("identity_secret is invalid")
    return decoded


def _clock_timestamp(clock: object) -> int:
    if not callable(clock):
        raise PlatformAdapterProtocolError("clock must be callable")
    try:
        raw = clock()
    except Exception:
        raise SteamConfirmationTransportError("steam_confirmation_clock_failed") from None
    if (
        type(raw) not in (int, float)
        or isinstance(raw, bool)
        or not math.isfinite(raw)
        or raw < 1
    ):
        raise SteamConfirmationTransportError("steam_confirmation_clock_invalid")
    return int(raw)


def _confirmation_key(secret: bytes, tag: str, timestamp: int) -> str:
    if type(tag) is not str or not tag or not tag.isascii():
        raise PlatformAdapterProtocolError("confirmation tag is invalid")
    payload = struct.pack(">Q", timestamp) + tag.encode("ascii")
    digest = hmac.new(secret, payload, digestmod=hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _device_id(steam_id: str) -> str:
    digest = hashlib.sha1(steam_id.encode("ascii")).hexdigest()
    return "android:" + "-".join(
        (
            digest[:8],
            digest[8:12],
            digest[12:16],
            digest[16:20],
            digest[20:32],
        )
    )


def _bounded_json_mapping(response: object, *, limit: int, write: bool) -> Mapping[str, Any]:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        raw = content
    elif isinstance(content, bytearray):
        raw = bytes(content)
    else:
        text = getattr(response, "text", None)
        if type(text) is not str:
            if write:
                raise SteamConfirmationWriteResultUnknown(
                    "steam_confirmation_write_result_unknown"
                )
            raise SteamConfirmationTransportError(
                "steam_confirmation_response_invalid"
            )
        raw = text.encode("utf-8")
    if len(raw) > limit:
        if write:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            )
        raise SteamConfirmationTransportError(
            "steam_confirmation_response_too_large"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        if write:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            ) from None
        raise SteamConfirmationTransportError(
            "steam_confirmation_malformed_response"
        ) from None
    if not isinstance(payload, Mapping):
        if write:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            )
        raise SteamConfirmationTransportError(
            "steam_confirmation_malformed_response"
        )
    return payload


class SteamTradeOfferConfirmationTransport:
    """Confirm exactly one proven mobile confirmation without retry or fallback."""

    __slots__ = (
        "_session",
        "_cookies",
        "_bound_account",
        "_identity_secret",
        "_timeout",
        "_clock",
        "_max_json_bytes",
        "_max_confirmations",
    )

    def __init__(
        self,
        cookie_string: str,
        identity_secret: str,
        *,
        session: object | None = None,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        clock=None,
        max_json_bytes: int = _DEFAULT_MAX_JSON_BYTES,
        max_confirmations: int = _DEFAULT_MAX_CONFIRMATIONS,
    ) -> None:
        cookies = _parse_cookie_string(cookie_string)
        bound_account = _secure_cookie_identity(cookies["steamLoginSecure"])
        secret = _identity_secret_bytes(identity_secret)
        client = session if session is not None else requests.Session()
        if getattr(client, "verify", None) is False:
            raise PlatformAdapterProtocolError("TLS verification must remain enabled")
        if not callable(getattr(client, "get", None)):
            raise PlatformAdapterProtocolError("session must provide GET")
        self._session = client
        self._cookies = dict(cookies)
        self._bound_account = bound_account
        self._identity_secret = secret
        self._timeout = _timeout_tuple(timeout)
        self._clock = time.time if clock is None else clock
        if not callable(self._clock):
            raise PlatformAdapterProtocolError("clock must be callable")
        self._max_json_bytes = _positive_limit(max_json_bytes, "max_json_bytes")
        self._max_confirmations = _positive_limit(
            max_confirmations, "max_confirmations"
        )

    @property
    def bound_account_steam_id(self) -> str:
        return self._bound_account

    def _params(self, tag: str) -> dict[str, object]:
        timestamp = _clock_timestamp(self._clock)
        return {
            "p": _device_id(self._bound_account),
            "a": self._bound_account,
            "k": _confirmation_key(self._identity_secret, tag, timestamp),
            "t": timestamp,
            "m": "android",
            "tag": tag,
        }

    def _read_get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float],
    ) -> Mapping[str, Any]:
        try:
            response = self._session.get(
                url,
                params=dict(params),
                headers=None if headers is None else dict(headers),
                cookies=self._cookies,
                timeout=timeout,
                allow_redirects=False,
            )
        except (requests.Timeout, TimeoutError):
            raise PlatformAdapterTimeoutError("steam_confirmation_read_timeout") from None
        except Exception:
            raise SteamConfirmationTransportError(
                "steam_confirmation_read_failure"
            ) from None
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            raise SteamConfirmationTransportAuthError(
                "steam_confirmation_auth_failed"
            )
        if type(status) is int and 300 <= status < 400:
            raise SteamConfirmationTransportAuthError(
                "steam_confirmation_auth_redirect"
            )
        if status != 200:
            raise SteamConfirmationTransportError(
                "steam_confirmation_read_failure"
            )
        return _bounded_json_mapping(
            response, limit=self._max_json_bytes, write=False
        )

    def _write_allow(
        self,
        confirmation_id: str,
        nonce: str,
        *,
        timeout: tuple[float, float],
    ) -> Mapping[str, Any]:
        params = self._params("allow")
        params.update(
            {
                "op": "allow",
                "cid": confirmation_id,
                "ck": nonce,
            }
        )
        try:
            response = self._session.get(
                _ALLOW_URL,
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
                cookies=self._cookies,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            ) from None
        if getattr(response, "status_code", None) != 200:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            )
        return _bounded_json_mapping(
            response, limit=self._max_json_bytes, write=True
        )

    def _confirmation_rows(
        self, *, timeout: tuple[float, float]
    ) -> tuple[tuple[str, str], ...]:
        payload = self._read_get(
            _GETLIST_URL,
            params=self._params("conf"),
            headers={
                "X-Requested-With": "com.valvesoftware.android.steam.community"
            },
            timeout=timeout,
        )
        if payload.get("success") is not True:
            raise SteamConfirmationTransportError(
                "steam_confirmation_list_unproven"
            )
        rows = payload.get("conf")
        if type(rows) is not list:
            raise SteamConfirmationTransportError(
                "steam_confirmation_list_malformed"
            )
        if len(rows) > self._max_confirmations:
            raise SteamConfirmationTransportError(
                "steam_confirmation_list_too_large"
            )
        normalized: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise SteamConfirmationTransportError(
                    "steam_confirmation_list_malformed"
                )
            try:
                confirmation_id = _canonical_positive_decimal(
                    row.get("id"), "confirmation_id"
                )
            except PlatformAdapterProtocolError:
                raise SteamConfirmationTransportError(
                    "steam_confirmation_list_malformed"
                ) from None
            nonce = row.get("nonce")
            if (
                type(nonce) is not str
                or not nonce
                or nonce.strip() != nonce
                or any(character.isspace() for character in nonce)
            ):
                raise SteamConfirmationTransportError(
                    "steam_confirmation_list_malformed"
                )
            if confirmation_id in seen_ids:
                raise SteamConfirmationTransportError(
                    "steam_confirmation_list_ambiguous"
                )
            seen_ids.add(confirmation_id)
            normalized.append((confirmation_id, nonce))
        return tuple(normalized)

    def _detail_trade_offer_id(
        self, confirmation_id: str, *, timeout: tuple[float, float]
    ) -> str | None:
        payload = self._read_get(
            _DETAILS_URL_PREFIX + confirmation_id,
            params=self._params("details" + confirmation_id),
            timeout=timeout,
        )
        if payload.get("success") is not True:
            raise SteamConfirmationTransportError(
                "steam_confirmation_detail_unproven"
            )
        html = payload.get("html")
        if type(html) is not str:
            raise SteamConfirmationTransportError(
                "steam_confirmation_detail_malformed"
            )
        matches = set(_TRADEOFFER_HTML_ID_RE.findall(html))
        if not matches:
            return None
        if len(matches) != 1:
            raise SteamConfirmationTransportError(
                "steam_confirmation_detail_ambiguous"
            )
        return _canonical_positive_decimal(
            next(iter(matches)), "detail_tradeoffer_id"
        )

    def confirm(
        self,
        steam_tradeoffer_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, str]:
        """Perform at most one allow mutation for one exact proven Trade Offer."""

        target = _canonical_positive_decimal(
            steam_tradeoffer_id, "steam_tradeoffer_id"
        )
        timeout = (
            self._timeout
            if timeout_seconds is None
            else _request_timeout(timeout_seconds)
        )
        matches: list[tuple[str, str]] = []
        for confirmation_id, nonce in self._confirmation_rows(timeout=timeout):
            detail_trade_offer_id = self._detail_trade_offer_id(
                confirmation_id, timeout=timeout
            )
            if detail_trade_offer_id == target:
                matches.append((confirmation_id, nonce))
        if not matches:
            raise SteamConfirmationTransportError(
                "steam_confirmation_exact_offer_not_found"
            )
        if len(matches) != 1:
            raise SteamConfirmationTransportError(
                "steam_confirmation_exact_offer_ambiguous"
            )

        confirmation_id, nonce = matches[0]
        payload = self._write_allow(
            confirmation_id, nonce, timeout=timeout
        )
        if payload.get("success") is not True:
            raise SteamConfirmationWriteResultUnknown(
                "steam_confirmation_write_result_unknown"
            )
        return {
            "steam_tradeoffer_id": target,
            "account_steam_id": self._bound_account,
        }


__all__ = [
    "SteamConfirmationTransportAuthError",
    "SteamConfirmationTransportError",
    "SteamConfirmationWriteResultUnknown",
    "SteamTradeOfferConfirmationTransport",
]
