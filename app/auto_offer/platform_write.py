"""Production-capable BUFF SEND_OFFER adapter, intentionally not runtime-wired.

The adapter obtains Steam cookies only at execution time, delegates exactly one
single-order POST to ``BuffBuyerSendTransport``, and never claims immediate
success because TASK-025 does not freeze a trustworthy response-side Steam
Trade Offer ID.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol, runtime_checkable

from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from buff.buyer_send import BuffBuyerSendError
from buff.request_policy import (
    BuffAuthExpired,
    BuffRequestBlocked,
    BuffWriteResultUnknown,
)

SEND_OFFER_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.SEND_OFFER}
)


@runtime_checkable
class BuyerSendTransport(Protocol):
    """One exact non-idempotent BUFF buyer-send operation."""

    def send(
        self,
        *,
        steam_cookie_string: str,
        buff_order_id: str,
        steam_id: str,
        timeout_seconds: float,
    ) -> dict:
        ...


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(
            f"{field} must be a non-whitespace string"
        )
    return value


def _require_steam_id(value: object) -> str:
    value = _require_identifier(value, "recipient_steam_id")
    if not value.isascii() or not value.isdecimal() or value[0] == "0":
        raise PlatformAdapterProtocolError("recipient_steam_id must be canonical")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise PlatformAdapterProtocolError("recipient_steam_id must be canonical")
    return value


def _result(
    request: PlatformRequest,
    status: PlatformResultStatus,
    detail: str,
) -> PlatformResult:
    return PlatformResult(request=request, status=status, detail=detail)


class BuffBuyerSendOfferAdapter:
    """Normalize one isolated BUFF buyer-send transport into PlatformResult."""

    __slots__ = (
        "_transport",
        "_account_id",
        "_recipient_steam_id",
        "_steam_cookie_provider",
    )

    def __init__(
        self,
        transport: BuyerSendTransport,
        *,
        account_id: str,
        recipient_steam_id: str,
        steam_cookie_provider: Callable[[], str],
    ) -> None:
        if not callable(getattr(transport, "send", None)):
            raise PlatformAdapterProtocolError("invalid_send_transport")
        if not callable(steam_cookie_provider):
            raise PlatformAdapterProtocolError("invalid_steam_cookie_provider")
        self._transport = transport
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _require_steam_id(recipient_steam_id)
        self._steam_cookie_provider = steam_cookie_provider

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return SEND_OFFER_CAPABILITIES

    def __repr__(self) -> str:
        return (
            "BuffBuyerSendOfferAdapter("
            f"account_id={self._account_id!r}, "
            f"recipient_steam_id={self._recipient_steam_id!r})"
        )

    def execute(self, request: PlatformRequest) -> PlatformResult:
        if type(request) is not PlatformRequest:
            raise PlatformAdapterProtocolError(
                "request must be a PlatformRequest"
            )
        PlatformRequest.__post_init__(request)

        if request.capability is not PlatformCapability.SEND_OFFER:
            return _result(
                request,
                PlatformResultStatus.UNSUPPORTED,
                "unsupported_capability",
            )
        if (
            request.account_id != self._account_id
            or request.recipient_steam_id != self._recipient_steam_id
        ):
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "identity_mismatch",
            )

        try:
            cookie_string = self._steam_cookie_provider()
        except Exception:
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "credential_unavailable",
            )
        if type(cookie_string) is not str or not cookie_string:
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "credential_unavailable",
            )

        try:
            response = self._transport.send(
                steam_cookie_string=cookie_string,
                buff_order_id=request.buff_order_id,
                steam_id=request.recipient_steam_id,
                timeout_seconds=request.timeout_seconds,
            )
        except BuffWriteResultUnknown:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        except (BuffAuthExpired, BuffRequestBlocked, BuffBuyerSendError):
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "write_preflight_failed",
            )
        except Exception:
            # The adapter cannot prove whether an arbitrary transport exception
            # happened before or after the underlying POST. Never classify it as
            # safe-to-retry.
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )

        if not isinstance(response, dict):
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        if response.get("code") == "OK":
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "offer_created_unproven",
            )
        return _result(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "write_result_unknown",
        )


__all__ = [
    "BuffBuyerSendOfferAdapter",
    "BuyerSendTransport",
    "SEND_OFFER_CAPABILITIES",
]
