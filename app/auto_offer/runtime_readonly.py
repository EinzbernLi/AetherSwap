"""Explicit default-off read-only runtime factory for native Auto Offer.

This module performs dependency injection only.  It does not load host config,
initialize persistence, attach to application startup, schedule work, retry,
poll, or expose any platform write capability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

from app.auto_offer import AUTO_OFFER_DEFAULT_ENABLED
from app.auto_offer.adapters import PlatformCapability
from app.auto_offer.coordinator import (
    ReadOnlyCoordinatorError,
    ReadOnlyDeliveryCoordinator,
    ReadOnlyStepResult,
)
from app.auto_offer.platform_readonly import (
    BuffReadOnlyAdapter,
    SteamCompletedTradeReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)
from app.auto_offer.steam_readonly_transport import (
    SteamCompletedTradeHttpReader,
    SteamTradeOfferHttpReader,
)


READONLY_RUNTIME_CAPABILITIES: Final[frozenset[PlatformCapability]] = frozenset(
    {
        PlatformCapability.READ_DELIVERY_DIRECTION,
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
    }
)


class ReadOnlyRuntimeConfigurationError(RuntimeError):
    """Sanitized failure to construct the explicit read-only runtime."""


def _strict_account_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ReadOnlyRuntimeConfigurationError("invalid_account_id")
    return value


def _canonical_positive_decimal(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise ReadOnlyRuntimeConfigurationError(f"invalid_{field}")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise ReadOnlyRuntimeConfigurationError(f"invalid_{field}")
    return value


def _positive_timeout(value: object) -> float:
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ReadOnlyRuntimeConfigurationError("invalid_timeout")
    return float(value)


@dataclass(frozen=True)
class ReadOnlyAutoOfferRuntime:
    """Thin one-shot wrapper around the reviewed read-only coordinator."""

    _coordinator: ReadOnlyDeliveryCoordinator = field(repr=False, compare=False)

    @property
    def capabilities(self) -> frozenset[PlatformCapability]:
        return READONLY_RUNTIME_CAPABILITIES

    def step(self, delivery: object) -> ReadOnlyStepResult:
        """Delegate exactly one delivery step to the existing coordinator."""
        return self._coordinator.step(delivery)


def build_readonly_auto_offer_runtime(
    *,
    enabled: bool = AUTO_OFFER_DEFAULT_ENABLED,
    store: object | None = None,
    buff_client: object | None = None,
    account_id: object = None,
    recipient_steam_id: object = None,
    steam_cookie_string: object = None,
    steam_session: object | None = None,
    timeout_seconds: float = 15.0,
) -> ReadOnlyAutoOfferRuntime | None:
    """Construct the module-owned read-only runtime only when explicitly enabled.

    The disabled path intentionally returns before inspecting any dependency.
    Enabled construction is local-only and performs no platform or Store I/O.
    """

    if type(enabled) is not bool:
        raise ReadOnlyRuntimeConfigurationError("enabled_must_be_bool")
    if enabled is False:
        return None

    account = _strict_account_id(account_id)
    recipient = _canonical_positive_decimal(recipient_steam_id, "recipient_steam_id")
    timeout = _positive_timeout(timeout_seconds)

    if store is None:
        raise ReadOnlyRuntimeConfigurationError("store_required")
    if buff_client is None:
        raise ReadOnlyRuntimeConfigurationError("buff_client_required")
    if steam_session is None:
        raise ReadOnlyRuntimeConfigurationError("steam_session_required")
    if type(steam_cookie_string) is not str or not steam_cookie_string:
        raise ReadOnlyRuntimeConfigurationError("steam_cookie_required")

    transport_timeout = (timeout, timeout)
    try:
        trade_offer_reader = SteamTradeOfferHttpReader(
            steam_cookie_string,
            session=steam_session,
            timeout=transport_timeout,
        )
        if trade_offer_reader.bound_account_steam_id != recipient:
            raise ReadOnlyRuntimeConfigurationError("steam_identity_mismatch")

        completed_trade_reader = SteamCompletedTradeHttpReader(
            steam_cookie_string,
            session=steam_session,
            timeout=transport_timeout,
        )
        if completed_trade_reader.bound_account_steam_id != recipient:
            raise ReadOnlyRuntimeConfigurationError("steam_identity_mismatch")

        buff_adapter = BuffReadOnlyAdapter(buff_client, account_id=account)
        trade_offer_adapter = SteamTradeOfferReadOnlyAdapter(
            trade_offer_reader,
            account_id=account,
            recipient_steam_id=recipient,
        )
        completed_trade_adapter = SteamCompletedTradeReadOnlyAdapter(
            completed_trade_reader,
            account_id=account,
            recipient_steam_id=recipient,
        )

        adapters = {
            PlatformCapability.READ_DELIVERY_DIRECTION: buff_adapter,
            PlatformCapability.READ_OFFER_STATE: buff_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: trade_offer_adapter,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE: completed_trade_adapter,
        }
        if frozenset(adapters) != READONLY_RUNTIME_CAPABILITIES:
            raise ReadOnlyRuntimeConfigurationError("readonly_capability_mismatch")

        coordinator = ReadOnlyDeliveryCoordinator(
            store,
            adapters,
            timeout_seconds=timeout,
        )
    except ReadOnlyRuntimeConfigurationError:
        raise
    except ReadOnlyCoordinatorError:
        raise ReadOnlyRuntimeConfigurationError("invalid_store_or_registry") from None
    except Exception:
        raise ReadOnlyRuntimeConfigurationError("invalid_readonly_dependency") from None

    return ReadOnlyAutoOfferRuntime(coordinator)


__all__ = [
    "READONLY_RUNTIME_CAPABILITIES",
    "ReadOnlyAutoOfferRuntime",
    "ReadOnlyRuntimeConfigurationError",
    "build_readonly_auto_offer_runtime",
]
