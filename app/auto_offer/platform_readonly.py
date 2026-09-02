"""Public fail-closed read-only adapter facade.

Ordinary construction remains exact-single-account binding. TASK-069 adds only
one recovery-specific choice: if the recovery Host builder finds one persisted
historical local lineage for the current Steam identity, the adapter binds to
that one persisted ``account_id`` instead of the current deployment-local key.
With no persisted target it remains bound to the current key; more than one
distinct persisted lineage fails closed. There is no multi-account dispatch,
bearer token, closure secret, frame provenance, or caller-identity boundary.

All parsing/evidence logic lives in ``_platform_readonly_core`` and remains
byte-identical to the pre-TASK-069 implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.auto_offer.adapters import PlatformAdapterProtocolError
from app.auto_offer import _platform_readonly_core as _core
from app.auto_offer._platform_readonly_core import *  # noqa: F401,F403


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")
    return value


@dataclass(frozen=True)
class _RecoveryAccountLineage:
    """The one exact local account binding selected for recovery-only reads."""

    current_account_id: str
    target_account_id: str

    @property
    def accepted_account_ids(self) -> frozenset[str]:
        return frozenset({self.current_account_id, self.target_account_id})


def _make_recovery_account_lineage(
    current_account_id: str,
    persisted_account_ids: frozenset[str],
) -> _RecoveryAccountLineage:
    """Choose current-or-one-persisted local binding; ambiguity fails closed.

    ``account_id`` is a local request/row lineage token, not an authentication
    credential. The Host builder separately proves the current registry Steam
    identity equals the current credential Steam identity. This helper only
    chooses the single local account ID that the read adapter binds to.
    """

    current = _require_identifier(current_account_id, "account_id")
    if type(persisted_account_ids) is not frozenset:
        raise PlatformAdapterProtocolError(
            "persisted account lineage must be immutable"
        )
    for account_id in persisted_account_ids:
        _require_identifier(account_id, "account_id")
    if len(persisted_account_ids) > 1:
        raise PlatformAdapterProtocolError(
            "recovery persisted account lineage is ambiguous"
        )
    target = current if not persisted_account_ids else next(iter(persisted_account_ids))
    return _RecoveryAccountLineage(current, target)


def _bound_account_id_for(
    account_id: str,
    recovery_lineage: _RecoveryAccountLineage | None,
) -> str:
    current = _require_identifier(account_id, "account_id")
    if recovery_lineage is None:
        return current
    if (
        type(recovery_lineage) is not _RecoveryAccountLineage
        or recovery_lineage.current_account_id != current
    ):
        raise PlatformAdapterProtocolError("recovery account lineage data is invalid")
    return _require_identifier(recovery_lineage.target_account_id, "account_id")


class BuffReadOnlyAdapter(_core.BuffReadOnlyAdapter):
    """Exact-binding BUFF adapter with one recovery-selected local account."""

    def __init__(
        self,
        client,
        *,
        account_id: str,
        historical_client=None,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        bound_account_id = _bound_account_id_for(account_id, recovery_lineage)
        super().__init__(
            client,
            account_id=bound_account_id,
            historical_client=historical_client,
        )


class SteamTradeOfferReadOnlyAdapter(_core.SteamTradeOfferReadOnlyAdapter):
    """Exact Steam offer reader with one recovery-selected local account."""

    def __init__(
        self,
        reader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        bound_account_id = _bound_account_id_for(account_id, recovery_lineage)
        recipient = _require_identifier(recipient_steam_id, "recipient_steam_id")
        super().__init__(
            reader,
            account_id=bound_account_id,
            recipient_steam_id=recipient,
        )


class SteamCompletedTradeReadOnlyAdapter(_core.SteamCompletedTradeReadOnlyAdapter):
    """Exact completed-trade reader with one recovery-selected local account."""

    def __init__(
        self,
        reader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        bound_account_id = _bound_account_id_for(account_id, recovery_lineage)
        recipient = _require_identifier(recipient_steam_id, "recipient_steam_id")
        super().__init__(
            reader,
            account_id=bound_account_id,
            recipient_steam_id=recipient,
        )


SteamInventoryReadOnlyAdapter = _core.SteamInventoryReadOnlyAdapter


def __getattr__(name: str):
    """Preserve access to unchanged implementation helpers for compatibility."""

    return getattr(_core, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_core)))


__all__ = list(_core.__all__)
