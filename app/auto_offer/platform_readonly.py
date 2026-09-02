"""Public fail-closed read-only adapter facade.

The ordinary adapter contract remains exact-single-account binding.  TASK-069
needs a narrowly scoped recovery bridge to preserve an existing persisted row's
local account lineage after deployment-local account re-keying.  That extra
lineage authority is represented by a sealed grant that can only be minted by
the exact recovery-only Host builder call site; normal callers cannot create
or inject arbitrary account aliases.

All parsing/evidence logic lives in ``_platform_readonly_core`` and remains
unchanged from the pre-TASK-069 implementation.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from app.auto_offer.adapters import PlatformAdapterProtocolError, PlatformRequest
from app.auto_offer import _platform_readonly_core as _core
from app.auto_offer._platform_readonly_core import *  # noqa: F401,F403


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise PlatformAdapterProtocolError(f"{field} must be a non-whitespace string")
    return value


def _build_sealed_recovery_lineage_surface():
    seal = object()

    @dataclass(frozen=True, init=False)
    class RecoveryAccountLineage:
        """Sealed immutable grant produced only by the recovery Host builder."""

        current_account_id: str
        accepted_account_ids: frozenset[str]

        def __init__(
            self,
            current_account_id: str,
            accepted_account_ids: frozenset[str],
            *,
            _seal: object | None = None,
        ) -> None:
            if _seal is not seal:
                raise PlatformAdapterProtocolError(
                    "recovery account lineage cannot be constructed directly"
                )
            current = _require_identifier(current_account_id, "account_id")
            if type(accepted_account_ids) is not frozenset or not accepted_account_ids:
                raise PlatformAdapterProtocolError(
                    "recovery account lineage must be a non-empty immutable set"
                )
            for account_id in accepted_account_ids:
                _require_identifier(account_id, "account_id")
            if current not in accepted_account_ids:
                raise PlatformAdapterProtocolError(
                    "recovery account lineage must include current account"
                )
            object.__setattr__(self, "current_account_id", current)
            object.__setattr__(self, "accepted_account_ids", accepted_account_ids)

    def make_recovery_account_lineage(
        current_account_id: str,
        persisted_account_ids: frozenset[str],
    ) -> RecoveryAccountLineage:
        """Mint one persisted-evidence lineage grant only from the exact Host builder."""

        current = _require_identifier(current_account_id, "account_id")
        if type(persisted_account_ids) is not frozenset:
            raise PlatformAdapterProtocolError(
                "persisted account lineage must be immutable"
            )
        for account_id in persisted_account_ids:
            _require_identifier(account_id, "account_id")

        frame = inspect.currentframe()
        caller = None if frame is None else frame.f_back
        try:
            from app.auto_offer import host_integration as host_integration

            builder = getattr(
                host_integration,
                "_build_recovery_only_host_auto_offer_bridge",
                None,
            )
            caller_ids = None if caller is None else caller.f_locals.get(
                "persisted_account_ids"
            )
            if (
                caller is None
                or builder is None
                or caller.f_code is not builder.__code__
                or caller.f_locals.get("account_id") != current
                or type(caller_ids) is not set
                or frozenset(caller_ids) != persisted_account_ids
            ):
                raise PlatformAdapterProtocolError(
                    "recovery account lineage factory is recovery-only"
                )
        finally:
            del frame
            del caller

        accepted = frozenset({current, *persisted_account_ids})
        return RecoveryAccountLineage(current, accepted, _seal=seal)

    return RecoveryAccountLineage, make_recovery_account_lineage


_RecoveryAccountLineage, _make_recovery_account_lineage = (
    _build_sealed_recovery_lineage_surface()
)
del _build_sealed_recovery_lineage_surface


def _accepted_account_ids_for(
    account_id: str,
    recovery_lineage: _RecoveryAccountLineage | None,
) -> frozenset[str]:
    current = _require_identifier(account_id, "account_id")
    if recovery_lineage is None:
        return frozenset({current})
    if (
        type(recovery_lineage) is not _RecoveryAccountLineage
        or recovery_lineage.current_account_id != current
    ):
        raise PlatformAdapterProtocolError(
            "recovery account lineage does not match current account"
        )
    return recovery_lineage.accepted_account_ids


class BuffReadOnlyAdapter(_core.BuffReadOnlyAdapter):
    """Exact-binding BUFF adapter with sealed recovery-only delegation."""

    def __init__(
        self,
        client,
        *,
        account_id: str,
        historical_client=None,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        current = _require_identifier(account_id, "account_id")
        super().__init__(
            client,
            account_id=current,
            historical_client=historical_client,
        )
        accepted = _accepted_account_ids_for(current, recovery_lineage)
        self._recovery_account_delegates = {
            alias: _core.BuffReadOnlyAdapter(
                client,
                account_id=alias,
                historical_client=historical_client,
            )
            for alias in accepted
            if alias != current
        }

    def execute(self, request: PlatformRequest):
        if type(request) is PlatformRequest:
            delegate = self._recovery_account_delegates.get(request.account_id)
            if delegate is not None:
                return delegate.execute(request)
        return super().execute(request)


class SteamTradeOfferReadOnlyAdapter(_core.SteamTradeOfferReadOnlyAdapter):
    """Exact Steam offer reader with sealed recovery-only local-lineage routing."""

    def __init__(
        self,
        reader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        current = _require_identifier(account_id, "account_id")
        recipient = _require_identifier(recipient_steam_id, "recipient_steam_id")
        super().__init__(
            reader,
            account_id=current,
            recipient_steam_id=recipient,
        )
        accepted = _accepted_account_ids_for(current, recovery_lineage)
        self._recovery_account_delegates = {
            alias: _core.SteamTradeOfferReadOnlyAdapter(
                reader,
                account_id=alias,
                recipient_steam_id=recipient,
            )
            for alias in accepted
            if alias != current
        }

    def execute(self, request: PlatformRequest):
        if type(request) is PlatformRequest:
            delegate = self._recovery_account_delegates.get(request.account_id)
            if delegate is not None:
                return delegate.execute(request)
        return super().execute(request)


class SteamCompletedTradeReadOnlyAdapter(_core.SteamCompletedTradeReadOnlyAdapter):
    """Exact completed-trade reader with sealed recovery-only lineage routing."""

    def __init__(
        self,
        reader,
        *,
        account_id: str,
        recipient_steam_id: str,
        recovery_lineage: _RecoveryAccountLineage | None = None,
    ) -> None:
        current = _require_identifier(account_id, "account_id")
        recipient = _require_identifier(recipient_steam_id, "recipient_steam_id")
        super().__init__(
            reader,
            account_id=current,
            recipient_steam_id=recipient,
        )
        accepted = _accepted_account_ids_for(current, recovery_lineage)
        self._recovery_account_delegates = {
            alias: _core.SteamCompletedTradeReadOnlyAdapter(
                reader,
                account_id=alias,
                recipient_steam_id=recipient,
            )
            for alias in accepted
            if alias != current
        }

    def execute(self, request: PlatformRequest):
        if type(request) is PlatformRequest:
            delegate = self._recovery_account_delegates.get(request.account_id)
            if delegate is not None:
                return delegate.execute(request)
        return super().execute(request)


# Recovery-only maintenance never needs inventory aliases. Keep that adapter
# exactly on the original singleton account contract.
SteamInventoryReadOnlyAdapter = _core.SteamInventoryReadOnlyAdapter


def __getattr__(name: str):
    """Preserve access to unchanged implementation helpers for compatibility."""

    return getattr(_core, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_core)))


__all__ = list(_core.__all__)
