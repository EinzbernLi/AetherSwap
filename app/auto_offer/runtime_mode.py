"""Pure runtime-mode contract for Auto Offer ownership switching.

This module separates the user's persisted enable/disable request from the
actual delivery authority mode.  It performs no I/O, platform action,
persistence, or worker scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AutoOfferRuntimeMode(str, Enum):
    OFF = "off"
    ENABLING = "enabling"
    ON = "on"
    DRAINING = "draining"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AutoOfferRuntimeState:
    requested_enabled: bool
    active_delivery_count: int
    mode: AutoOfferRuntimeMode
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.requested_enabled) is not bool:
            raise TypeError("requested_enabled must be bool")
        if type(self.active_delivery_count) is not int or self.active_delivery_count < 0:
            raise ValueError("active_delivery_count must be a non-negative integer")
        if type(self.mode) is not AutoOfferRuntimeMode:
            raise TypeError("mode must be AutoOfferRuntimeMode")
        if self.reason is not None and (
            type(self.reason) is not str
            or not self.reason
            or self.reason.strip() != self.reason
        ):
            raise ValueError("reason must be a non-whitespace string when present")


def resolve_runtime_mode(
    *,
    requested_enabled: bool,
    active_delivery_count: int,
    enable_preflight_passed: bool = False,
    transition_block_reason: str | None = None,
) -> AutoOfferRuntimeState:
    """Resolve effective ownership without changing any external state.

    An enable request remains ENABLING until the caller positively proves the
    transition preflight.  A disable request with active Auto Offer ownership
    enters DRAINING rather than immediately returning legacy authority.  A
    transition blocker is explicit instead of being collapsed into the
    persisted boolean.
    """

    if type(requested_enabled) is not bool:
        raise TypeError("requested_enabled must be bool")
    if type(active_delivery_count) is not int or active_delivery_count < 0:
        raise ValueError("active_delivery_count must be a non-negative integer")
    if type(enable_preflight_passed) is not bool:
        raise TypeError("enable_preflight_passed must be bool")
    if transition_block_reason is not None and (
        type(transition_block_reason) is not str
        or not transition_block_reason
        or transition_block_reason.strip() != transition_block_reason
    ):
        raise ValueError("transition_block_reason must be a non-whitespace string")

    if transition_block_reason is not None:
        return AutoOfferRuntimeState(
            requested_enabled=requested_enabled,
            active_delivery_count=active_delivery_count,
            mode=AutoOfferRuntimeMode.BLOCKED,
            reason=transition_block_reason,
        )
    if requested_enabled:
        return AutoOfferRuntimeState(
            requested_enabled=True,
            active_delivery_count=active_delivery_count,
            mode=(
                AutoOfferRuntimeMode.ON
                if enable_preflight_passed
                else AutoOfferRuntimeMode.ENABLING
            ),
        )
    if active_delivery_count:
        return AutoOfferRuntimeState(
            requested_enabled=False,
            active_delivery_count=active_delivery_count,
            mode=AutoOfferRuntimeMode.DRAINING,
        )
    return AutoOfferRuntimeState(
        requested_enabled=False,
        active_delivery_count=0,
        mode=AutoOfferRuntimeMode.OFF,
    )


__all__ = [
    "AutoOfferRuntimeMode",
    "AutoOfferRuntimeState",
    "resolve_runtime_mode",
]
