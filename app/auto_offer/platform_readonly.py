"""Public fail-closed read-only adapter facade.

Ordinary construction remains exact-single-account binding. TASK-069 permits
additional historical local lineage only when construction occurs through the
real public recovery-maintenance builder and its real private bridge builder,
after current local-account and Steam credential identity admission. No bearer
secret is used or exposed; a forged lineage object has no authority by itself.

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


@dataclass(frozen=True, init=False)
class _RecoveryAccountLineage:
    """Immutable recovery lineage data; authority comes from builder provenance."""

    current_account_id: str
    accepted_account_ids: frozenset[str]

    def __new__(cls, *args, **kwargs):
        raise PlatformAdapterProtocolError(
            "recovery account lineage cannot be constructed directly"
        )


def _recovery_builder_context_matches(private_frame, current_account_id: str) -> bool:
    if private_frame is None:
        return False
    public_frame = private_frame.f_back
    if public_frame is None:
        return False

    from app.auto_offer import host_integration as host_integration

    private_builder = getattr(
        host_integration,
        "_build_recovery_only_host_auto_offer_bridge",
        None,
    )
    public_builder = getattr(
        host_integration,
        "build_host_recovery_only_maintenance",
        None,
    )
    module_globals = vars(host_integration)
    if private_builder is None or public_builder is None:
        return False
    if (
        private_frame.f_code is not private_builder.__code__
        or private_frame.f_globals is not module_globals
        or private_frame.f_globals.get("_build_recovery_only_host_auto_offer_bridge")
        is not private_builder
        or public_frame.f_code is not public_builder.__code__
        or public_frame.f_globals is not module_globals
        or public_frame.f_globals.get("build_host_recovery_only_maintenance")
        is not public_builder
    ):
        return False

    private_locals = private_frame.f_locals
    public_locals = public_frame.f_locals
    return (
        private_locals.get("account_id") == current_account_id
        and public_locals.get("account_id") == current_account_id
        and private_locals.get("account_steam_id")
        == public_locals.get("account_steam_id")
        and private_locals.get("steam_cookie_string")
        == public_locals.get("cookie_string")
        and private_locals.get("buff_client") is public_locals.get("buff_client")
        and private_locals.get("store_path") == public_locals.get("store_path")
    )


def _make_recovery_account_lineage(
    current_account_id: str,
    persisted_account_ids: frozenset[str],
) -> _RecoveryAccountLineage:
    """Create lineage data only after the exact public recovery admission chain."""

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
        caller_ids = None if caller is None else caller.f_locals.get(
            "persisted_account_ids"
        )
        if (
            not _recovery_builder_context_matches(caller, current)
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
    lineage = object.__new__(_RecoveryAccountLineage)
    object.__setattr__(lineage, "current_account_id", current)
    object.__setattr__(lineage, "accepted_account_ids", accepted)
    return lineage


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
        or type(recovery_lineage.accepted_account_ids) is not frozenset
        or current not in recovery_lineage.accepted_account_ids
    ):
        raise PlatformAdapterProtocolError(
            "recovery account lineage data is invalid"
        )

    frame = inspect.currentframe()
    adapter_frame = None if frame is None else frame.f_back
    builder_frame = None if adapter_frame is None else adapter_frame.f_back
    try:
        persisted_ids = None if builder_frame is None else builder_frame.f_locals.get(
            "persisted_account_ids"
        )
        builder_lineage = None if builder_frame is None else builder_frame.f_locals.get(
            "recovery_lineage"
        )
        expected = (
            None
            if type(persisted_ids) is not set
            else frozenset({current, *persisted_ids})
        )
        if (
            not _recovery_builder_context_matches(builder_frame, current)
            or builder_lineage is not recovery_lineage
            or expected != recovery_lineage.accepted_account_ids
        ):
            raise PlatformAdapterProtocolError(
                "recovery account lineage is recovery-builder-only"
            )
    finally:
        del frame
        del adapter_frame
        del builder_frame

    return recovery_lineage.accepted_account_ids


class BuffReadOnlyAdapter(_core.BuffReadOnlyAdapter):
    """Exact-binding BUFF adapter with recovery-builder-only delegation."""

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
    """Exact Steam offer reader with recovery-builder-only lineage routing."""

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
    """Exact completed-trade reader with recovery-builder-only lineage routing."""

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


SteamInventoryReadOnlyAdapter = _core.SteamInventoryReadOnlyAdapter


def __getattr__(name: str):
    """Preserve access to unchanged implementation helpers for compatibility."""

    return getattr(_core, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_core)))


__all__ = list(_core.__all__)
