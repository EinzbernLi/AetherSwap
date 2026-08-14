"""Exact, capability-minimized incoming Steam Trade Offer ACCEPT adapter.

The adapter performs no discovery, retry, polling, bulk action or persistence.
Coordinator + Store must durably record OFFER_ACCEPT_ATTEMPTED before invoking
this boundary. A write ambiguity is always RESULT_UNKNOWN and is never retried.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from app.auto_offer.adapters import (
    AcceptOfferEvidence,
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)


ACCEPT_OFFER_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {PlatformCapability.ACCEPT_OFFER}
)


class AcceptOfferPreflightError(RuntimeError):
    """The exact ACCEPT was rejected before its POST could be entered."""


class AcceptOfferWriteResultUnknown(RuntimeError):
    """The ACCEPT POST may have executed but its result cannot be proven."""


@runtime_checkable
class IncomingOfferAcceptTransport(Protocol):
    """One exact incoming-offer ACCEPT transport."""

    def accept(
        self,
        *,
        steam_tradeoffer_id: str,
        account_steam_id: str,
        counterparty_steam_id: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        ...


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(
            f"{field} must be a non-whitespace string"
        )
    return value


def _canonical_steam_id(value: object, field: str) -> str:
    value = _require_identifier(value, field)
    if not value.isascii() or not value.isdecimal() or value[0] == "0":
        raise PlatformAdapterProtocolError(f"{field} must be canonical")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise PlatformAdapterProtocolError(f"{field} must be canonical")
    return value


def _result(
    request: PlatformRequest,
    status: PlatformResultStatus,
    detail: str,
    evidence: AcceptOfferEvidence | None = None,
) -> PlatformResult:
    return PlatformResult(
        request=request,
        status=status,
        detail=detail,
        evidence=evidence,
    )


class SteamIncomingOfferAcceptAdapter:
    """Normalize exactly one bound incoming ACCEPT into a PlatformResult."""

    __slots__ = (
        "_transport",
        "_account_id",
        "_recipient_steam_id",
        "_counterparty_steam_id",
    )

    def __init__(
        self,
        transport: IncomingOfferAcceptTransport,
        *,
        account_id: str,
        recipient_steam_id: str,
        expected_counterparty_steam_id: str | None = None,
    ) -> None:
        if not callable(getattr(transport, "accept", None)):
            raise PlatformAdapterProtocolError("invalid_accept_transport")
        self._transport = transport
        self._account_id = _require_identifier(account_id, "account_id")
        self._recipient_steam_id = _canonical_steam_id(
            recipient_steam_id,
            "recipient_steam_id",
        )
        self._counterparty_steam_id = (
            None
            if expected_counterparty_steam_id is None
            else _canonical_steam_id(
                expected_counterparty_steam_id,
                "expected_counterparty_steam_id",
            )
        )
        if self._recipient_steam_id == self._counterparty_steam_id:
            raise PlatformAdapterProtocolError(
                "recipient and counterparty Steam IDs must differ"
            )

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return ACCEPT_OFFER_CAPABILITIES

    def __repr__(self) -> str:
        return (
            "SteamIncomingOfferAcceptAdapter("
            f"account_id={self._account_id!r}, "
            f"recipient_steam_id={self._recipient_steam_id!r}, "
            f"counterparty_steam_id={self._counterparty_steam_id!r})"
        )

    def execute(self, request: PlatformRequest) -> PlatformResult:
        if type(request) is not PlatformRequest:
            raise PlatformAdapterProtocolError(
                "request must be a PlatformRequest"
            )
        PlatformRequest.__post_init__(request)
        if request.capability is not PlatformCapability.ACCEPT_OFFER:
            return _result(
                request,
                PlatformResultStatus.UNSUPPORTED,
                "unsupported_capability",
            )
        if (
            request.account_id != self._account_id
            or request.recipient_steam_id != self._recipient_steam_id
            or (
                self._counterparty_steam_id is not None
                and request.counterparty_steam_id
                != self._counterparty_steam_id
            )
        ):
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "identity_mismatch",
            )

        try:
            response = self._transport.accept(
                steam_tradeoffer_id=request.steam_tradeoffer_id or "",
                account_steam_id=request.recipient_steam_id,
                counterparty_steam_id=request.counterparty_steam_id or "",
                timeout_seconds=request.timeout_seconds,
            )
        except AcceptOfferPreflightError:
            return _result(
                request,
                PlatformResultStatus.FAILURE,
                "write_preflight_failed",
            )
        except AcceptOfferWriteResultUnknown:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        except Exception:
            # Once this boundary is entered an arbitrary exception cannot prove
            # that the non-idempotent POST was not transmitted.
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
        if response.get("accepted") is False:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        if response.get("accepted") is not True:
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        if (
            response.get("steam_tradeoffer_id") != request.steam_tradeoffer_id
            or response.get("account_steam_id") != request.recipient_steam_id
        ):
            return _result(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "write_result_unknown",
            )
        return _result(
            request,
            PlatformResultStatus.SUCCESS,
            "accept_returned_success",
            AcceptOfferEvidence(
                steam_tradeoffer_id=request.steam_tradeoffer_id or "",
                account_steam_id=request.recipient_steam_id,
            ),
        )


__all__ = [
    "ACCEPT_OFFER_CAPABILITIES",
    "AcceptOfferPreflightError",
    "AcceptOfferWriteResultUnknown",
    "IncomingOfferAcceptTransport",
    "SteamIncomingOfferAcceptAdapter",
]
