"""Strict GET-only Steam completed-trade transport for Auto Offer.

The transport owns no login, refresh, persistence, retry, background work, or
Steam mutation.  It converts one exact accepted Trade Offer into the normalized
mapping consumed by ``SteamCompletedTradeReadOnlyAdapter``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

import requests

from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
)

_STEAM_ID64_BASE = 76561197960265728
_MAX_ACCOUNT_ID = (1 << 32) - 1
_ACCEPTED_TRADE_OFFER_STATE = 3
_GET_TRADE_OFFER_URL = "https://api.steampowered.com/IEconService/GetTradeOffer/v1/"
_STEAM_COMMUNITY = "https://steamcommunity.com"
_DEFAULT_TIMEOUT = (5.0, 15.0)
_DEFAULT_INVENTORY_COUNT = 5000
_DEFAULT_MAX_INVENTORY_PAGES = 20
_DEFAULT_MAX_RECEIPT_BYTES = 1_000_000
_DEFAULT_MAX_RECEIPT_OBJECTS = 256
_DEFAULT_MAX_JSON_BYTES = 2_000_000
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENCODED_SECURE_SEPARATOR_RE = re.compile(r"%7c%7c", re.IGNORECASE)
_URL_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_RECEIPT_URL_RE = re.compile(
    r"^https://steamcommunity\.com/trade/[1-9][0-9]*/receipt$"
)
_INVENTORY_URL_RE = re.compile(
    r"^https://steamcommunity\.com/inventory/"
    r"[1-9][0-9]*/[1-9][0-9]*/[A-Za-z0-9._~-]+$"
)


class SteamReadOnlyTransportError(RuntimeError):
    """Sanitized read-side transport failure."""


class SteamReadOnlyTransportAuthError(SteamReadOnlyTransportError):
    """Sanitized authentication/session failure."""


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


def _strict_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")
    return value


def _safe_url_path_segment(value: object, field: str) -> str:
    value = _strict_identifier(value, field)
    if not value.isascii() or _URL_PATH_SEGMENT_RE.fullmatch(value) is None:
        raise PlatformAdapterProtocolError(
            f"{field} must be a safe URL path segment"
        )
    return value


def _require_allowed_read_url(url: object) -> str:
    if type(url) is not str:
        raise PlatformAdapterProtocolError("Steam read URL is not allowlisted")
    if url == _GET_TRADE_OFFER_URL:
        return url
    if _RECEIPT_URL_RE.fullmatch(url):
        return url
    if _INVENTORY_URL_RE.fullmatch(url):
        return url
    raise PlatformAdapterProtocolError("Steam read URL is not allowlisted")


def _positive_int(value: object, field: str) -> int:
    if type(value) is int:
        normalized = value
    elif type(value) is str:
        normalized = int(_canonical_positive_decimal(value, field))
    else:
        raise PlatformAdapterProtocolError(f"{field} must be a positive integer")
    if normalized <= 0:
        raise PlatformAdapterProtocolError(f"{field} must be a positive integer")
    return normalized


def _finite_non_negative(value: object, field: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise PlatformAdapterProtocolError(
            f"{field} must be a finite non-negative number"
        )
    return float(value)


def _positive_limit(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlatformAdapterProtocolError(f"{field} must be a positive integer")
    return value


def _timeout_tuple(value: object) -> tuple[float, float]:
    if type(value) is not tuple or len(value) != 2:
        raise PlatformAdapterProtocolError("timeout must be a (connect, read) tuple")
    values: list[float] = []
    for part in value:
        if type(part) not in (int, float) or not math.isfinite(part) or part <= 0:
            raise PlatformAdapterProtocolError("timeout values must be finite and positive")
        values.append(float(part))
    return values[0], values[1]


def _parse_cookie_string(cookie_string: object) -> dict[str, str]:
    if type(cookie_string) is not str or not cookie_string.strip():
        raise PlatformAdapterProtocolError("cookie_string must be a non-empty string")
    cookies: dict[str, str] = {}
    for raw_part in cookie_string.split(";"):
        # Leading whitespace immediately after ';' is ordinary cookie-header
        # formatting.  Do not normalize any whitespace belonging to the
        # cookie name or value itself.
        part = raw_part.lstrip()
        if not part:
            continue
        if part != part.rstrip():
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        if "=" not in part:
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        name, _, value = part.partition("=")
        if (
            not name
            or name != name.strip()
            or not _COOKIE_NAME_RE.fullmatch(name)
            or not value
            or value != value.strip()
            or name in cookies
        ):
            raise PlatformAdapterProtocolError("cookie_string is malformed")
        cookies[name] = value
    if "steamLoginSecure" not in cookies:
        raise PlatformAdapterProtocolError("steamLoginSecure cookie is required")
    return cookies


def _secure_cookie_identity(secure_cookie: str) -> tuple[str, str]:
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
    return steam_id, access_token


def _account_id_to_steam_id64(value: object) -> str:
    if type(value) is int:
        account_id = value
    elif type(value) is str:
        account_id = int(_canonical_positive_decimal(value, "accountid_other"))
    else:
        raise PlatformAdapterProtocolError("accountid_other is malformed")
    if account_id <= 0 or account_id > _MAX_ACCOUNT_ID:
        raise PlatformAdapterProtocolError("accountid_other is malformed")
    return str(_STEAM_ID64_BASE + account_id)


def _normalize_source_items(
    value: object, field: str
) -> tuple[dict[str, object], ...]:
    if type(value) is not list:
        raise PlatformAdapterProtocolError(f"{field} must be a list")
    items: list[dict[str, object]] = []
    identities: set[tuple[int, str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise PlatformAdapterProtocolError(f"{field} contains malformed item")
        required = ("appid", "contextid", "assetid", "amount")
        if any(key not in raw for key in required):
            raise PlatformAdapterProtocolError(f"{field} contains malformed item")
        item = {
            "appid": _positive_int(raw["appid"], "appid"),
            "contextid": _strict_identifier(raw["contextid"], "contextid"),
            "assetid": _strict_identifier(raw["assetid"], "assetid"),
            "amount": _positive_int(raw["amount"], "amount"),
        }
        identity = (item["appid"], item["contextid"], item["assetid"])
        if identity in identities:
            raise PlatformAdapterProtocolError(f"{field} contains duplicate source identity")
        identities.add(identity)
        items.append(item)
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item["appid"], item["contextid"], item["assetid"], item["amount"]
            ),
        )
    )


def _bounded_response_bytes(response: object, *, limit: int) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        raw = content
    elif isinstance(content, bytearray):
        raw = bytes(content)
    else:
        text = getattr(response, "text", None)
        if type(text) is not str:
            raise SteamReadOnlyTransportError("steam_read_failure")
        raw = text.encode("utf-8")
    if len(raw) > limit:
        raise SteamReadOnlyTransportError("steam_response_too_large")
    return raw


def _json_mapping(response: object, *, limit: int) -> Mapping[str, Any]:
    raw = _bounded_response_bytes(response, limit=limit)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise SteamReadOnlyTransportError("steam_malformed_response") from None
    if not isinstance(payload, Mapping):
        raise SteamReadOnlyTransportError("steam_malformed_response")
    return payload


def _receipt_objects(html: str, *, max_objects: int) -> tuple[Mapping[str, Any], ...]:
    """Independently extract bounded ``oItem = {...}`` JSON objects."""

    objects: list[Mapping[str, Any]] = []
    cursor = 0
    marker = "oItem"
    length = len(html)
    while cursor < length:
        start = html.find(marker, cursor)
        if start < 0:
            break
        index = start + len(marker)
        while index < length and html[index].isspace():
            index += 1
        if index >= length or html[index] != "=":
            cursor = start + len(marker)
            continue
        index += 1
        while index < length and html[index].isspace():
            index += 1
        if index >= length or html[index] != "{":
            cursor = index
            continue

        object_start = index
        depth = 0
        in_string = False
        escaped = False
        object_end: int | None = None
        while index < length:
            character = html[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
            else:
                if character == '"':
                    in_string = True
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        object_end = index + 1
                        break
                    if depth < 0:
                        raise SteamReadOnlyTransportError("steam_malformed_receipt")
            index += 1
        if object_end is None or in_string or depth != 0:
            raise SteamReadOnlyTransportError("steam_malformed_receipt")
        if len(objects) >= max_objects:
            raise SteamReadOnlyTransportError("steam_receipt_object_limit")
        try:
            decoded = json.loads(html[object_start:object_end])
        except Exception:
            raise SteamReadOnlyTransportError("steam_malformed_receipt") from None
        if not isinstance(decoded, Mapping):
            raise SteamReadOnlyTransportError("steam_malformed_receipt")
        objects.append(decoded)
        cursor = object_end
    return tuple(objects)


def _receipt_source_identity(raw: Mapping[str, Any]) -> tuple[int, str, str, int] | None:
    required = ("appid", "contextid", "assetid", "amount")
    if any(key not in raw for key in required):
        return None
    try:
        return (
            _positive_int(raw["appid"], "appid"),
            _strict_identifier(raw["contextid"], "contextid"),
            _strict_identifier(raw["assetid"], "assetid"),
            _positive_int(raw["amount"], "amount"),
        )
    except PlatformAdapterProtocolError:
        return None


def _map_receipt_items(
    source_items: tuple[dict[str, object], ...],
    receipt_items: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, object], ...] | None:
    mapped: list[dict[str, object]] = []
    for source in source_items:
        source_identity = (
            source["appid"], source["contextid"], source["assetid"], source["amount"]
        )
        matches = [
            raw for raw in receipt_items if _receipt_source_identity(raw) == source_identity
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise PlatformAdapterProtocolError("receipt contains duplicate source identity")
        raw = matches[0]
        has_new_context = "new_contextid" in raw
        has_new_asset = "new_assetid" in raw
        if not has_new_context or not has_new_asset:
            raise PlatformAdapterProtocolError("receipt source mapping is incomplete")
        mapped.append(
            {
                **source,
                "new_contextid": _strict_identifier(raw["new_contextid"], "new_contextid"),
                "new_assetid": _strict_identifier(raw["new_assetid"], "new_assetid"),
            }
        )
    return tuple(
        sorted(
            mapped,
            key=lambda item: (
                item["appid"], item["contextid"], item["assetid"],
                item["new_contextid"], item["new_assetid"], item["amount"],
            ),
        )
    )


class SteamCompletedTradeHttpReader:
    """One exact, bounded, authenticated Steam completed-trade reader."""

    def __init__(
        self,
        cookie_string: str,
        *,
        session: object | None = None,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        inventory_count: int = _DEFAULT_INVENTORY_COUNT,
        max_inventory_pages: int = _DEFAULT_MAX_INVENTORY_PAGES,
        max_receipt_bytes: int = _DEFAULT_MAX_RECEIPT_BYTES,
        max_receipt_objects: int = _DEFAULT_MAX_RECEIPT_OBJECTS,
        max_json_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    ) -> None:
        cookies = _parse_cookie_string(cookie_string)
        bound_account, access_token = _secure_cookie_identity(cookies["steamLoginSecure"])
        client = session if session is not None else requests.Session()
        if getattr(client, "verify", None) is False:
            raise PlatformAdapterProtocolError("TLS verification must remain enabled")
        if not callable(getattr(client, "get", None)):
            raise PlatformAdapterProtocolError("session must provide GET")
        self._session = client
        self._community_cookies = dict(cookies)
        self._bound_account = bound_account
        self._access_token = access_token
        self._timeout = _timeout_tuple(timeout)
        self._inventory_count = _positive_limit(inventory_count, "inventory_count")
        self._max_inventory_pages = _positive_limit(max_inventory_pages, "max_inventory_pages")
        self._max_receipt_bytes = _positive_limit(max_receipt_bytes, "max_receipt_bytes")
        self._max_receipt_objects = _positive_limit(max_receipt_objects, "max_receipt_objects")
        self._max_json_bytes = _positive_limit(max_json_bytes, "max_json_bytes")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bound_account={self._bound_account!r})"

    @property
    def bound_account_steam_id(self) -> str:
        return self._bound_account

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        community: bool,
    ) -> object:
        url = _require_allowed_read_url(url)
        kwargs: dict[str, object] = {
            "params": params,
            "timeout": self._timeout,
            "allow_redirects": False,
        }
        if community:
            kwargs["cookies"] = self._community_cookies
        try:
            response = self._session.get(url, **kwargs)
        except (requests.Timeout, TimeoutError):
            raise PlatformAdapterTimeoutError("steam_read_timeout") from None
        except Exception:
            raise SteamReadOnlyTransportError("steam_read_failure") from None
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            raise SteamReadOnlyTransportAuthError("steam_auth_failed")
        if type(status) is int and 300 <= status < 400:
            raise SteamReadOnlyTransportAuthError("steam_auth_redirect")
        if status != 200:
            raise SteamReadOnlyTransportError("steam_read_failure")
        return response

    def _read_offer(
        self, steam_tradeoffer_id: str
    ) -> tuple[
        str,
        str,
        float,
        tuple[dict[str, object], ...],
        tuple[dict[str, object], ...],
    ] | None:
        response = self._get(
            _GET_TRADE_OFFER_URL,
            params={
                "access_token": self._access_token,
                "tradeofferid": steam_tradeoffer_id,
                "language": "english",
            },
            community=False,
        )
        payload = _json_mapping(response, limit=self._max_json_bytes)
        response_payload = payload.get("response")
        if not isinstance(response_payload, Mapping):
            raise SteamReadOnlyTransportError("steam_malformed_response")
        offer = response_payload.get("offer")
        if not isinstance(offer, Mapping):
            raise SteamReadOnlyTransportError("steam_malformed_response")
        if offer.get("tradeofferid") != steam_tradeoffer_id:
            raise PlatformAdapterProtocolError("returned tradeofferid does not match request")
        state = offer.get("trade_offer_state")
        if type(state) is not int:
            raise PlatformAdapterProtocolError("trade_offer_state is malformed")
        if state != _ACCEPTED_TRADE_OFFER_STATE:
            return None
        trade_id = offer.get("tradeid")
        if trade_id in (None, ""):
            return None
        trade_id = _canonical_positive_decimal(trade_id, "tradeid")
        if type(offer.get("is_our_offer")) is not bool:
            raise PlatformAdapterProtocolError("is_our_offer is malformed")
        counterparty = _account_id_to_steam_id64(offer.get("accountid_other"))
        completed_at = _finite_non_negative(offer.get("time_updated"), "time_updated")
        items_given = _normalize_source_items(offer.get("items_to_give"), "items_to_give")
        items_received = _normalize_source_items(
            offer.get("items_to_receive"), "items_to_receive"
        )
        return trade_id, counterparty, completed_at, items_given, items_received

    def _read_receipt(self, trade_id: str) -> tuple[Mapping[str, Any], ...] | None:
        response = self._get(
            f"{_STEAM_COMMUNITY}/trade/{trade_id}/receipt",
            community=True,
        )
        raw = _bounded_response_bytes(response, limit=self._max_receipt_bytes)
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SteamReadOnlyTransportError("steam_malformed_receipt") from None
        objects = _receipt_objects(html, max_objects=self._max_receipt_objects)
        return objects or None

    def _inventory_confirmed(
        self,
        recipient_steam_id: str,
        received_item: Mapping[str, object],
    ) -> tuple[dict[str, object], ...]:
        appid = received_item["appid"]
        contextid = _safe_url_path_segment(
            received_item["new_contextid"],
            "new_contextid",
        )
        assetid = received_item["new_assetid"]
        amount = received_item["amount"]
        url = f"{_STEAM_COMMUNITY}/inventory/{recipient_steam_id}/{appid}/{contextid}"
        seen_cursors: set[str] = set()
        previous_cursor: int | None = None
        for _ in range(self._max_inventory_pages):
            params: dict[str, object] = {
                "l": "english",
                "count": self._inventory_count,
            }
            if previous_cursor is not None:
                params["start_assetid"] = str(previous_cursor)
            response = self._get(url, params=params, community=True)
            payload = _json_mapping(response, limit=self._max_json_bytes)
            if type(payload.get("success")) is not int or payload.get("success") != 1:
                raise SteamReadOnlyTransportError("steam_malformed_inventory")
            assets = payload.get("assets", [])
            if type(assets) is not list:
                raise SteamReadOnlyTransportError("steam_malformed_inventory")
            for raw_asset in assets:
                if not isinstance(raw_asset, Mapping):
                    raise SteamReadOnlyTransportError("steam_malformed_inventory")
                required = ("appid", "contextid", "assetid", "amount")
                if any(key not in raw_asset for key in required):
                    raise SteamReadOnlyTransportError("steam_malformed_inventory")
                current = (
                    _positive_int(raw_asset["appid"], "appid"),
                    _strict_identifier(raw_asset["contextid"], "contextid"),
                    _strict_identifier(raw_asset["assetid"], "assetid"),
                    _positive_int(raw_asset["amount"], "amount"),
                )
                if current == (appid, contextid, assetid, amount):
                    return (
                        {
                            "appid": appid,
                            "contextid": contextid,
                            "assetid": assetid,
                            "amount": amount,
                        },
                    )
            more_items = payload.get("more_items", 0)
            if type(more_items) is not int or more_items not in (0, 1):
                raise SteamReadOnlyTransportError("steam_malformed_inventory")
            if more_items == 0:
                return ()
            last_assetid = payload.get("last_assetid")
            if type(last_assetid) is int:
                if last_assetid <= 0:
                    raise SteamReadOnlyTransportError("steam_malformed_inventory")
                cursor = str(last_assetid)
            elif type(last_assetid) is str:
                cursor = _canonical_positive_decimal(last_assetid, "last_assetid")
            else:
                raise SteamReadOnlyTransportError("steam_malformed_inventory")
            cursor_value = int(cursor)
            if cursor in seen_cursors or (
                previous_cursor is not None and cursor_value <= previous_cursor
            ):
                raise SteamReadOnlyTransportError("steam_inventory_pagination_cycle")
            seen_cursors.add(cursor)
            previous_cursor = cursor_value
        raise SteamReadOnlyTransportError("steam_inventory_page_limit")

    def __call__(self, steam_tradeoffer_id: str, recipient_steam_id: str) -> object:
        steam_tradeoffer_id = _canonical_positive_decimal(
            steam_tradeoffer_id, "steam_tradeoffer_id"
        )
        recipient_steam_id = _canonical_positive_decimal(
            recipient_steam_id, "recipient_steam_id"
        )
        if recipient_steam_id != self._bound_account:
            raise PlatformAdapterProtocolError(
                "recipient_steam_id does not match authenticated Steam account"
            )
        offer = self._read_offer(steam_tradeoffer_id)
        if offer is None:
            return None
        trade_id, counterparty, completed_at, source_given, source_received = offer
        if counterparty == recipient_steam_id:
            raise PlatformAdapterProtocolError(
                "counterparty Steam ID cannot equal recipient Steam ID"
            )
        if not source_received:
            return None
        receipt = self._read_receipt(trade_id)
        if receipt is None:
            return None
        items_given = _map_receipt_items(source_given, receipt)
        items_received = _map_receipt_items(source_received, receipt)
        if items_given is None or items_received is None:
            return None
        inventory_confirmed_items: tuple[dict[str, object], ...] = ()
        if not items_given and len(items_received) == 1:
            inventory_confirmed_items = self._inventory_confirmed(
                recipient_steam_id, items_received[0]
            )
        return {
            "steam_tradeoffer_id": steam_tradeoffer_id,
            "steam_trade_id": trade_id,
            "account_steam_id": recipient_steam_id,
            "counterparty_steam_id": counterparty,
            "completed_at": completed_at,
            "items_given": list(items_given),
            "items_received": list(items_received),
            "inventory_confirmed_items": list(inventory_confirmed_items),
        }


class SteamTradeOfferHttpReader(SteamCompletedTradeHttpReader):
    """One exact, bounded, authenticated Steam Trade Offer lifecycle reader."""

    def __init__(
        self,
        cookie_string: str,
        *,
        session: object | None = None,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
        max_json_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    ) -> None:
        super().__init__(
            cookie_string,
            session=session,
            timeout=timeout,
            max_json_bytes=max_json_bytes,
        )

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        community: bool,
    ) -> object:
        if community or url != _GET_TRADE_OFFER_URL:
            raise PlatformAdapterProtocolError(
                "Steam Trade Offer read URL is not allowlisted"
            )
        return super()._get(url, params=params, community=False)

    def __call__(self, steam_tradeoffer_id: str) -> object:
        steam_tradeoffer_id = _canonical_positive_decimal(
            steam_tradeoffer_id, "steam_tradeoffer_id"
        )
        response = self._get(
            _GET_TRADE_OFFER_URL,
            params={
                "access_token": self._access_token,
                "tradeofferid": steam_tradeoffer_id,
                "language": "english",
            },
            community=False,
        )
        payload = _json_mapping(response, limit=self._max_json_bytes)
        response_payload = payload.get("response")
        if not isinstance(response_payload, Mapping):
            raise SteamReadOnlyTransportError("steam_malformed_response")
        offer = response_payload.get("offer")
        if not isinstance(offer, Mapping):
            raise SteamReadOnlyTransportError("steam_malformed_response")
        if offer.get("tradeofferid") != steam_tradeoffer_id:
            raise PlatformAdapterProtocolError("returned tradeofferid does not match request")

        state = offer.get("trade_offer_state")
        if type(state) is not int:
            raise PlatformAdapterProtocolError("trade_offer_state is malformed")
        if type(offer.get("is_our_offer")) is not bool:
            raise PlatformAdapterProtocolError("is_our_offer is malformed")
        counterparty = _account_id_to_steam_id64(offer.get("accountid_other"))
        if counterparty == self._bound_account:
            raise PlatformAdapterProtocolError(
                "counterparty Steam ID cannot equal authenticated Steam account"
            )
        items_given = _normalize_source_items(offer.get("items_to_give"), "items_to_give")
        items_received = _normalize_source_items(
            offer.get("items_to_receive"), "items_to_receive"
        )
        if not items_given and not items_received:
            raise PlatformAdapterProtocolError("trade offer must contain at least one item")

        lifecycle = {2: "active", 3: "accepted"}.get(state)
        if lifecycle is None:
            return None
        return {
            "steam_tradeoffer_id": steam_tradeoffer_id,
            "account_steam_id": self._bound_account,
            "counterparty_steam_id": counterparty,
            "is_our_offer": offer["is_our_offer"],
            "lifecycle": lifecycle,
            "items_to_give": list(items_given),
            "items_to_receive": list(items_received),
        }


__all__ = [
    "SteamCompletedTradeHttpReader",
    "SteamTradeOfferHttpReader",
    "SteamReadOnlyTransportAuthError",
    "SteamReadOnlyTransportError",
]
