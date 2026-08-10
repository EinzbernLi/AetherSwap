"""Single-order BUFF buyer-send transport with fail-closed crypto handling.

This module owns no session, credentials, retries, runtime wiring, or persistence.
It wraps an already-owned BuffBuyer-like client and delegates the one POST through
its existing hardened ``_make_request`` policy.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

API_BUYER_SEND_OFFER = (
    "https://buff.163.com/api/market/manual_plus/buyer_send_offer"
)
_BUYER_SEND_REFERER = (
    "https://buff.163.com/market/buy_order/to_receive?game=csgo"
)
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENCODED_SECURE_SEPARATOR_RE = re.compile(r"%7c%7c", re.IGNORECASE)

# Public interoperability material observed in the frozen BUFF-compatible
# reference. This is not a secret and is kept isolated from credential data.
_BUFF_BUYER_INFO_PUBLIC_KEY_B64 = (
    "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEArF75iD8PXTT+B5nAnnhw"
    "qxg9I48t9uED7r6GuRcPYUZ0Ye3Vdvs71CVjuELyxALtj5cN+Pe1DwDSUAH1TF+9"
    "dS7769gcJaMMdgEB6vyssm9fnPKB4KXqbUHdMT1MF2tylemDlqfsfpkV91wtAhHf"
    "SkNtsQcPw4Juhn0IK+2xyvlm6HtXqFOkhial5T+miGBJk3snHfLPmQFsg/3EuHFM"
    "tBzoLX29C46SNv/W33dwOk3mgIP1SMy4TLmm8CuyNiCuHPum53Q3RXSGrpR2nJps"
    "4ICIWb0P3VZmPhCrDK1iWwwtVGj9jDkCT2zh+B18j26vfTkBDdac5s4sw739uAha"
    "bH56BQflowPICHVWtptCEnORewxo/FDhFUtn4sjiQswgnTHJ6F/q0vwegRRsx0AT"
    "f3SvpksR6dZuUqHzISthooQ/68PrJ8VaKfT17u43pif08/bFkZAkYdLev4Mk0SlZ"
    "YOqpRoif+7Pi0yObTZ0bgpCwDb1kgAmqCHi9pFPS/LUMVqSqMa4maxAX2A8a/cbl"
    "CJbjBHLn0zrZn3YW4hKlaVvGFG/Mmag+ALV5xII0y6JSoqdxlxpyhEmbOi/GCFMw"
    "0Mn6lyvYDCvYVwS7UqLMw7NU3WXhbNUh8DgBSb5jo4yY9E42d24JiumZulzkSdgy"
    "OSkVea8JGUUD8PliMtRJOQkCAwEAAQ=="
)


class BuffBuyerSendError(RuntimeError):
    """Sanitized local buyer-send validation or crypto failure."""


@runtime_checkable
class BuffBuyerRequestClient(Protocol):
    """The existing BUFF client surface required by this transport."""

    steam_id: str

    def _make_request(self, method: str, url: str, **kwargs) -> dict:
        ...


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise BuffBuyerSendError(f"invalid_{field}")
    return value


def _require_steam_id(value: object) -> str:
    value = _require_identifier(value, "steam_id")
    if not value.isascii() or not value.isdecimal() or value[0] == "0":
        raise BuffBuyerSendError("invalid_steam_id")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise BuffBuyerSendError("invalid_steam_id")
    return value


def _require_timeout(value: object) -> float:
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise BuffBuyerSendError("invalid_timeout")
    return float(value)


def _parse_cookie_string(cookie_string: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_part in cookie_string.split(";"):
        part = raw_part.lstrip()
        if not part:
            continue
        if part != part.rstrip() or "=" not in part:
            raise BuffBuyerSendError("invalid_steam_cookie")
        name, _, value = part.partition("=")
        if (
            not name
            or name != name.strip()
            or _COOKIE_NAME_RE.fullmatch(name) is None
            or not value
            or value != value.strip()
            or name in parsed
        ):
            raise BuffBuyerSendError("invalid_steam_cookie")
        parsed[name] = value
    return parsed


def _secure_cookie_steam_id(secure_cookie: str) -> str:
    raw_count = secure_cookie.count("||")
    encoded_matches = list(_ENCODED_SECURE_SEPARATOR_RE.finditer(secure_cookie))
    if raw_count == 1 and not encoded_matches:
        steam_id, token = secure_cookie.split("||", 1)
    elif raw_count == 0 and len(encoded_matches) == 1:
        match = encoded_matches[0]
        steam_id = secure_cookie[: match.start()]
        token = secure_cookie[match.end() :]
    else:
        raise BuffBuyerSendError("steam_login_secure_invalid")
    steam_id = _require_steam_id(steam_id)
    if not token or token != token.strip():
        raise BuffBuyerSendError("steam_login_secure_invalid")
    return steam_id


def _require_steam_cookie_string(value: object, *, expected_steam_id: str) -> str:
    cookie_string = _require_identifier(value, "steam_cookie")
    parsed = _parse_cookie_string(cookie_string)
    secure_cookie = parsed.get("steamLoginSecure")
    if secure_cookie is None:
        raise BuffBuyerSendError("steam_login_secure_required")
    if _secure_cookie_steam_id(secure_cookie) != expected_steam_id:
        raise BuffBuyerSendError("steam_cookie_identity_mismatch")
    return cookie_string


def _decode_public_key(public_key_b64: str):
    try:
        der = base64.b64decode(public_key_b64, validate=True)
        key = RSA.import_key(der)
    except (ValueError, TypeError, IndexError, binascii.Error):
        raise BuffBuyerSendError("buyer_info_public_key_invalid") from None
    if key.size_in_bits() < 2048:
        raise BuffBuyerSendError("buyer_info_public_key_invalid")
    return key.public_key()


def encrypt_buyer_info(
    steam_cookie_string: str,
    *,
    expected_steam_id: str,
    public_key_b64: str = _BUFF_BUYER_INFO_PUBLIC_KEY_B64,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> str:
    """Encrypt one identity-bound Steam cookie using the frozen envelope."""

    expected_steam_id = _require_steam_id(expected_steam_id)
    steam_cookie_string = _require_steam_cookie_string(
        steam_cookie_string,
        expected_steam_id=expected_steam_id,
    )
    if not callable(random_bytes):
        raise BuffBuyerSendError("buyer_info_random_source_invalid")
    public_key = _decode_public_key(public_key_b64)
    try:
        aes_key = random_bytes(16)
        iv = random_bytes(16)
    except Exception:
        raise BuffBuyerSendError("buyer_info_random_source_failed") from None
    if type(aes_key) is not bytes or len(aes_key) != 16:
        raise BuffBuyerSendError("buyer_info_random_source_invalid")
    if type(iv) is not bytes or len(iv) != 16:
        raise BuffBuyerSendError("buyer_info_random_source_invalid")

    try:
        encrypted_key = PKCS1_v1_5.new(public_key).encrypt(aes_key)
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(
            pad(steam_cookie_string.encode("utf-8"), AES.block_size)
        )
    except Exception:
        raise BuffBuyerSendError("buyer_info_encryption_failed") from None

    return base64.b64encode(encrypted_key + iv + ciphertext).decode("ascii")


class BuffBuyerSendTransport:
    """Perform exactly one buyer-send POST through an already-owned BUFF client."""

    __slots__ = ("_client",)

    def __init__(self, client: BuffBuyerRequestClient) -> None:
        sender = getattr(client, "_make_request", None)
        if not callable(sender):
            raise BuffBuyerSendError("buff_client_write_method_required")
        self._client = client

    def send(
        self,
        *,
        steam_cookie_string: str,
        buff_order_id: str,
        steam_id: str,
        timeout_seconds: float,
    ) -> dict:
        """Issue one non-idempotent POST after completing all local preflight."""

        order_id = _require_identifier(buff_order_id, "buff_order_id")
        steam_id = _require_steam_id(steam_id)
        timeout = _require_timeout(timeout_seconds)
        cookie_string = _require_steam_cookie_string(
            steam_cookie_string,
            expected_steam_id=steam_id,
        )

        bound_steam_id = getattr(self._client, "steam_id", "")
        if bound_steam_id:
            bound_steam_id = _require_steam_id(bound_steam_id)
            if bound_steam_id != steam_id:
                raise BuffBuyerSendError("steam_identity_mismatch")

        encrypted_info = encrypt_buyer_info(
            cookie_string,
            expected_steam_id=steam_id,
        )
        payload = {
            "buyer_info": encrypted_info,
            "bill_orders": [order_id],
            "steamid": steam_id,
        }
        result = self._client._make_request(
            "POST",
            API_BUYER_SEND_OFFER,
            json=payload,
            timeout=timeout,
            headers={"Referer": _BUYER_SEND_REFERER},
        )
        if not isinstance(result, dict):
            raise BuffBuyerSendError("buyer_send_response_invalid")
        return result


__all__ = [
    "API_BUYER_SEND_OFFER",
    "BuffBuyerRequestClient",
    "BuffBuyerSendError",
    "BuffBuyerSendTransport",
    "encrypt_buyer_info",
]
