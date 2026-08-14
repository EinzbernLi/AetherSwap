"""Effective Auto Offer runtime authority inspection.

The persisted ``auto_offer.enabled`` value is user intent only.  This module
combines that intent with the current Host/Store ownership snapshot and the
existing safety fences, then delegates the final mode choice to the pure
``runtime_mode`` resolver.  Inspection is read-only and never initializes the
Store or performs a platform action.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .canary_authority import CanaryAuthorityError, canary_metadata_present
from .contracts import DeliveryStatus
from .host_integration import is_auto_offer_enabled
from .host_ownership import (
    HostPurchaseOwnership,
    classify_host_purchase,
)
from .runtime_mode import (
    AutoOfferRuntimeMode,
    AutoOfferRuntimeState,
    resolve_runtime_mode,
)
from .store import AutoOfferStore, AutoOfferStoreError, StoredDelivery


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"
_AUDIT_ONLY_STATUSES = frozenset(
    {
        DeliveryStatus.RECEIVED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }
)
_PROTECTED_STORE_STATUSES = frozenset(
    status for status in DeliveryStatus if status not in _AUDIT_ONLY_STATUSES
)


def _empty_asset(value: object) -> bool:
    return value is None or value == ""


def _requested_from_config(config: Mapping[str, object] | None) -> bool:
    if config is None:
        from app.config_loader import load_app_config_validated

        config = load_app_config_validated()
    return is_auto_offer_enabled(config)


def _host_snapshot(purchases: Sequence[Mapping[str, object]] | None):
    if purchases is not None:
        return purchases
    from app.state import get_purchases

    return get_purchases()


def _pipeline_is_active() -> bool:
    try:
        from app.state import get_status

        status = get_status() or {}
        if status.get("status") == "running" or status.get("step") == "CHECKOUT_PENDING":
            return True
    except Exception:
        pass
    try:
        from app.pipeline import is_pipeline_running

        return bool(is_pipeline_running())
    except Exception:
        return False


def _reconciliation_blocked(
    *,
    unresolved_checkout: object,
    pipeline_active: bool | None,
) -> bool:
    if unresolved_checkout is None:
        return False
    if pipeline_active is None:
        pipeline_active = _pipeline_is_active()
    return not pipeline_active


def _store_snapshot(
    store_path: str | Path,
    store_rows: Sequence[StoredDelivery] | None,
) -> list[StoredDelivery]:
    if store_rows is not None:
        return list(store_rows)
    return AutoOfferStore.inspect_existing(store_path)


def _safe_blocked(requested_enabled: bool, active_count: int, reason: str):
    return resolve_runtime_mode(
        requested_enabled=requested_enabled,
        active_delivery_count=active_count,
        transition_block_reason=reason,
    )


def inspect_effective_runtime(
    *,
    config: Mapping[str, object] | None = None,
    requested_enabled: bool | None = None,
    purchases: Sequence[Mapping[str, object]] | None = None,
    store_path: str | Path | None = None,
    store_rows: Sequence[StoredDelivery] | None = None,
    canary_fenced: bool | None = None,
    unresolved_checkout: object | None = None,
    pipeline_active: bool | None = None,
    reconciliation_checked: bool = False,
) -> AutoOfferRuntimeState:
    """Return one sanitized, read-only effective authority snapshot.

    Optional arguments are dependency-injection seams for tests and callers
    that already hold a current Host snapshot.  Normal production callers use
    the canonical config, Host State, Store inspector, canary fence, and BUFF
    checkout guard directly.
    """

    try:
        if requested_enabled is None:
            requested_enabled = _requested_from_config(config)
        if type(requested_enabled) is not bool:
            return _safe_blocked(False, 0, "runtime_request_invalid")

        host_rows = _host_snapshot(purchases)
        if not isinstance(host_rows, Sequence) or isinstance(host_rows, (str, bytes)):
            return _safe_blocked(requested_enabled, 0, "host_snapshot_invalid")

        path = _STORE_PATH if store_path is None else Path(store_path)
        rows = _store_snapshot(path, store_rows)
        index: dict[str, StoredDelivery] = {}
        for stored in rows:
            if type(stored) is not StoredDelivery:
                return _safe_blocked(requested_enabled, 0, "store_row_invalid")
            order_id = stored.snapshot.buff_order_id
            if type(order_id) is not str or not order_id or order_id.strip() != order_id:
                return _safe_blocked(requested_enabled, 0, "store_identity_invalid")
            if (
                stored.snapshot.purchase_id != f"buff:{order_id}"
                or type(stored.revision) is not int
                or stored.revision <= 0
            ):
                return _safe_blocked(
                    requested_enabled,
                    0,
                    "store_identity_invalid",
                )
            if order_id in index:
                return _safe_blocked(requested_enabled, 0, "duplicate_store_order_identity")
            index[order_id] = stored

        host_order_ids: set[str] = set()
        decisions = []
        for purchase in host_rows:
            if not isinstance(purchase, Mapping):
                return _safe_blocked(requested_enabled, 0, "host_snapshot_invalid")
            order_id = purchase.get("buff_order_id")
            if order_id not in (None, "") and (
                type(order_id) is not str
                or not order_id
                or order_id.strip() != order_id
                or any(ord(character) < 32 for character in order_id)
            ):
                return _safe_blocked(
                    requested_enabled,
                    0,
                    "host_identity_invalid",
                )
            if isinstance(order_id, str) and order_id:
                if order_id in host_order_ids:
                    return _safe_blocked(
                        requested_enabled,
                        0,
                        "duplicate_host_order_identity",
                    )
                host_order_ids.add(order_id)
            decisions.append(classify_host_purchase(purchase, store_index=index))

        for order_id, stored in index.items():
            if (
                stored.snapshot.delivery_status in _PROTECTED_STORE_STATUSES
                and order_id not in host_order_ids
            ):
                return _safe_blocked(
                    requested_enabled,
                    0,
                    "active_orphan_store",
                )

        active_count = 0
        legacy_pending = False
        blocked_delivery = False
        for purchase, decision in zip(host_rows, decisions):
            if decision.ownership in {
                HostPurchaseOwnership.MANAGED,
                HostPurchaseOwnership.RECEIPT_PENDING,
            }:
                active_count += 1
                if (
                    decision.stored is not None
                    and decision.stored.snapshot.delivery_status is DeliveryStatus.BLOCKED
                ):
                    blocked_delivery = True
            elif decision.ownership is HostPurchaseOwnership.UNSAFE:
                return _safe_blocked(
                    requested_enabled,
                    active_count,
                    "host_store_ownership_unsafe",
                )
            elif (
                decision.ownership is HostPurchaseOwnership.UNOWNED
                and purchase.get("pending_receipt") is True
                and _empty_asset(purchase.get("assetid"))
            ):
                legacy_pending = True

        if canary_fenced is None:
            try:
                canary_fenced = bool(canary_metadata_present())
            except CanaryAuthorityError:
                return _safe_blocked(requested_enabled, active_count, "canary_fenced")
            except Exception:
                return _safe_blocked(requested_enabled, active_count, "canary_fenced")
        if canary_fenced:
            return _safe_blocked(requested_enabled, active_count, "canary_fenced")

        if not reconciliation_checked:
            if unresolved_checkout is None:
                try:
                    from app.services.buff_checkout_guard import get_unresolved_checkout

                    unresolved_checkout = get_unresolved_checkout()
                except Exception:
                    return _safe_blocked(
                        requested_enabled,
                        active_count,
                        "buff_reconciliation_required",
                    )
            if _reconciliation_blocked(
                unresolved_checkout=unresolved_checkout,
                pipeline_active=pipeline_active,
            ):
                return _safe_blocked(
                    requested_enabled,
                    active_count,
                    "buff_reconciliation_required",
                )

        if requested_enabled and legacy_pending:
            return _safe_blocked(
                requested_enabled,
                active_count,
                "legacy_pending_unowned",
            )

        if blocked_delivery:
            return _safe_blocked(
                requested_enabled,
                active_count,
                "delivery_blocked",
            )

        return resolve_runtime_mode(
            requested_enabled=requested_enabled,
            active_delivery_count=active_count,
            enable_preflight_passed=True,
        )
    except (AutoOfferStoreError, OSError):
        return _safe_blocked(
            bool(requested_enabled) if type(requested_enabled) is bool else False,
            0,
            "store_unreadable",
        )
    except Exception:
        return _safe_blocked(
            bool(requested_enabled) if type(requested_enabled) is bool else False,
            0,
            "runtime_inspection_failed",
        )


def get_effective_runtime_state(**kwargs) -> AutoOfferRuntimeState:
    """Production-named façade used by Host worker, pipeline, and status."""

    return inspect_effective_runtime(**kwargs)


def resolve_effective_runtime(**kwargs) -> AutoOfferRuntimeState:
    """Compatibility alias for callers that use resolver terminology."""

    return inspect_effective_runtime(**kwargs)


def preflight_auto_offer_enable(
    *,
    config: Mapping[str, object] | None = None,
    purchases: Sequence[Mapping[str, object]] | None = None,
    store_path: str | Path | None = None,
    store_rows: Sequence[StoredDelivery] | None = None,
) -> AutoOfferRuntimeState:
    """Run a positive, read-only OFF->ON authority preflight."""

    state = inspect_effective_runtime(
        config=config,
        requested_enabled=True,
        purchases=purchases,
        store_path=store_path,
        store_rows=store_rows,
    )
    if state.mode is not AutoOfferRuntimeMode.ON:
        return state
    return state


def runtime_state_payload(state: AutoOfferRuntimeState) -> dict[str, object]:
    """Return the stable, secret-free backend status representation."""

    return {
        "requested_enabled": state.requested_enabled,
        "mode": state.mode.value,
        "active_delivery_count": state.active_delivery_count,
        "reason": state.reason,
    }


inspect_runtime_state = inspect_effective_runtime
resolve_effective_runtime_state = inspect_effective_runtime
get_runtime_state = get_effective_runtime_state


__all__ = [
    "AutoOfferRuntimeMode",
    "AutoOfferRuntimeState",
    "get_effective_runtime_state",
    "get_runtime_state",
    "inspect_effective_runtime",
    "inspect_runtime_state",
    "preflight_auto_offer_enable",
    "resolve_effective_runtime",
    "resolve_effective_runtime_state",
    "runtime_state_payload",
]
