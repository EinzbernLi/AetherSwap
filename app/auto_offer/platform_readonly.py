"""Public fail-closed read-only adapter facade.

Ordinary construction remains exact-single-account binding. TASK-069 adds only
one recovery-specific choice: when the recovery Host builder proves a single
persisted historical lineage for the current Steam identity, the adapter binds
to that one persisted ``account_id`` instead of the current deployment-local
registry key. It does not accept multiple account aliases and it does not rely
on bearer tokens, closure secrets, frame provenance, or caller identity.

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
    """One exact persisted local lineage selected for recovery-only reads."""

    current_account_id: str
    target_account_id: str

    @property
    def accepted_account_ids(self) -> frozenset[str]:
        return frozenset({self.current_account_id, self.target_account_id})


def _make_recovery_account_lineage(
    current_account_id: str,
    persisted_account_ids: frozenset[str],
) -> _RecoveryAccountLineage:
    """Select exactly one persisted recovery lineage or fail closed.

    ``account_id`` is a local request/row lineage token, not an authentication
    credential. The current Steam identity and credential identity are proven
    by the Host recovery builder before this helper is called. This helper does
    not create authority; it only chooses which one exact local lineage the
    already read-only adapter instance will bind to.
    """

    current = _require_identifier(current_account_id, "account_id")
    if type(persisted_account_ids) is not frozenset:
        raise PlatformAdapterProtocolError(
            "persisted account lineage must be immutable"
        )
    for account_id in persisted_account_ids:
        _require_identifier(account_id, "account_id")
    if len(persisted_account_ids) != 1:
        raise PlatformAdapterProtocolError(
            "recovery persisted account lineage must be unique"
        )
    target = next(iter(persisted_account_ids))
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
    """Exact-binding BUFF adapter; recovery may select one persisted lineage."""

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
    """Exact Steam offer reader with one recovery-local lineage binding."""

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
    """Exact completed-trade reader with one recovery-local lineage binding."""

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
