"""Pure mapping from exact Steam reader lifecycle strings to typed evidence."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import SteamTradeOfferLifecycle


class SteamLifecycleEvidenceError(ValueError):
    pass


_LIFECYCLES = {
    "active": SteamTradeOfferLifecycle.ACTIVE,
    "accepted": SteamTradeOfferLifecycle.ACCEPTED,
    "created_needs_confirmation": SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
    "countered": SteamTradeOfferLifecycle.COUNTERED,
    "expired": SteamTradeOfferLifecycle.EXPIRED,
    "canceled": SteamTradeOfferLifecycle.CANCELED,
    "cancelled": SteamTradeOfferLifecycle.CANCELED,
    "declined": SteamTradeOfferLifecycle.DECLINED,
    "invalid_items": SteamTradeOfferLifecycle.INVALID_ITEMS,
    "canceled_by_second_factor": SteamTradeOfferLifecycle.CANCELED_BY_SECOND_FACTOR,
    "in_escrow": SteamTradeOfferLifecycle.IN_ESCROW,
}


@dataclass(frozen=True)
class SteamLifecycleEvidence:
    lifecycle: SteamTradeOfferLifecycle
    detail: str

    @property
    def terminal_without_trade(self) -> bool:
        return self.lifecycle.is_terminal_without_trade


def map_exact_steam_lifecycle(value: object) -> SteamLifecycleEvidence:
    if type(value) is not str or not value or value.strip() != value:
        raise SteamLifecycleEvidenceError("trade_offer_state_not_proven")
    lifecycle = _LIFECYCLES.get(value)
    if lifecycle is None:
        raise SteamLifecycleEvidenceError("trade_offer_state_not_proven")
    return SteamLifecycleEvidence(
        lifecycle=lifecycle,
        detail=f"trade_offer_{lifecycle.value}",
    )


__all__ = [
    "SteamLifecycleEvidence",
    "SteamLifecycleEvidenceError",
    "map_exact_steam_lifecycle",
]
