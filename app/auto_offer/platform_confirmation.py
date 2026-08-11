"""CONFIRM_OFFER-only Steam mobile confirmation platform adapter.

The adapter is intentionally not registered in the runtime Coordinator in
TASK-029. It normalizes one exact confirmation transport call and never retries.
"""

from __future__ import annotations

from app.auto_offer.adapters import (
    ConfirmOfferEvidence,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.steam_confirmation_transport import (
    SteamConfirmationTransportAuthError,
    SteamConfirmationTransportError,
    SteamConfirmationWriteResultUnknown,
)


_CONFIRM_CAPABILITIES = frozenset({PlatformCapability.CONFIRM_OFFER})


class SteamTradeOfferConfirmationAdapter:
    """Normalize one exact Steam mobile confirmation attempt."""

    def __init__(
        self,
        transport,
        *,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        if (
            type(account_id) is not str
            or not account_id
            or account_id.strip() != account_id
        ):
            raise PlatformAdapterProtocolError(
                "account_id must be a non-whitespace string"
            )
        if (
            type(recipient_steam_id) is not str
            or not recipient_steam_id
            or recipient_steam_id.strip() != recipient_steam_id
        ):
            raise PlatformAdapterProtocolError(
                "recipient_steam_id must be a non-whitespace string"
            )
        confirm = getattr(transport, "confirm", None)
        if not callable(confirm):
            raise PlatformAdapterProtocolError("transport must provide confirm")
        bound_account = getattr(transport, "bound_account_steam_id", None)
        if bound_account != recipient_steam_id:
            raise PlatformAdapterProtocolError(
                "transport Steam identity does not match recipient_steam_id"
            )
        self._transport = transport
        self._account_id = account_id
        self._recipient_steam_id = recipient_steam_id

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return _CONFIRM_CAPABILITIES

    def execute(self, request: PlatformRequest) -> PlatformResult:
        if type(request) is not PlatformRequest:
            raise PlatformAdapterProtocolError(
                "request must be a PlatformRequest"
            )
        PlatformRequest.__post_init__(request)
        if request.capability is not PlatformCapability.CONFIRM_OFFER:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.UNSUPPORTED,
                detail="unsupported_capability",
            )
        if (
            request.account_id != self._account_id
            or request.recipient_steam_id != self._recipient_steam_id
        ):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.FAILURE,
                detail="identity_mismatch",
            )

        try:
            raw = self._transport.confirm(
                request.steam_tradeoffer_id,
                timeout_seconds=request.timeout_seconds,
            )
        except SteamConfirmationWriteResultUnknown:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.RESULT_UNKNOWN,
                detail="confirmation_result_unknown",
            )
        except PlatformAdapterTimeoutError:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.TIMEOUT,
                detail="confirmation_read_timeout",
            )
        except SteamConfirmationTransportAuthError:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.FAILURE,
                detail="confirmation_auth_failed",
            )
        except SteamConfirmationTransportError:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.FAILURE,
                detail="confirmation_preflight_failed",
            )
        except PlatformAdapterProtocolError:
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.MALFORMED,
                detail="confirmation_protocol_error",
            )
        except Exception:
            # An untyped transport exception cannot prove whether the mutation
            # boundary was crossed. Never classify it as safe-to-retry.
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.RESULT_UNKNOWN,
                detail="confirmation_result_unknown",
            )

        if not isinstance(raw, dict):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.MALFORMED,
                detail="confirmation_result_invalid",
            )
        if (
            raw.get("steam_tradeoffer_id") != request.steam_tradeoffer_id
            or raw.get("account_steam_id") != request.recipient_steam_id
        ):
            return PlatformResult(
                request=request,
                status=PlatformResultStatus.MALFORMED,
                detail="confirmation_result_identity_mismatch",
            )
        return PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="trade_offer_mobile_confirmed",
            evidence=ConfirmOfferEvidence(
                steam_tradeoffer_id=request.steam_tradeoffer_id,
                account_steam_id=request.recipient_steam_id,
            ),
        )


__all__ = ["SteamTradeOfferConfirmationAdapter"]
