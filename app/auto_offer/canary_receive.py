"""Receive-thread-owned execution for one captured fresh-canary target.

This module is deliberately not a scheduler.  It is called only by the existing
Host receive worker through :mod:`canary_takeover`.  Every call provisions one
generation-aware BUFF facade and one ordinary Host Auto Offer integration in the
calling thread, runs one bounded delivery tick, then closes both in that same
thread.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .contracts import AutoOfferResult, DeliveryStatus
from .host_integration import (
    DeliveryTickOutcome,
    HostAutoOfferIntegrationError,
    build_host_auto_offer_integration,
)
from .runtime_mode import AutoOfferRuntimeMode
from .store import StoredDelivery


class CanaryReceiveTickError(RuntimeError):
    """Stable secret-free fail-closed reason for receive-owned canary work."""


@dataclass(frozen=True)
class CanaryReceiveTarget:
    host_db_id: int
    buff_order_id: str
    purchase_id: str
    account_id: str
    recipient_steam_id: str

    def __post_init__(self) -> None:
        if type(self.host_db_id) is not int or self.host_db_id <= 0:
            raise CanaryReceiveTickError("canary_target_invalid")
        for field in (
            "buff_order_id",
            "purchase_id",
            "account_id",
            "recipient_steam_id",
        ):
            value = getattr(self, field)
            if type(value) is not str or not value or value.strip() != value:
                raise CanaryReceiveTickError("canary_target_invalid")
        if self.purchase_id != f"buff:{self.buff_order_id}":
            raise CanaryReceiveTickError("canary_target_invalid")


@dataclass(frozen=True)
class CanaryReceiveTickResult:
    outcome: DeliveryTickOutcome
    stored: StoredDelivery
    runtime_mode: AutoOfferRuntimeMode
    reason: str | None = None


def _validate_target_stored(
    stored: object,
    target: CanaryReceiveTarget,
) -> StoredDelivery:
    if type(stored) is not StoredDelivery:
        raise CanaryReceiveTickError("canary_store_target_missing")
    snapshot = stored.snapshot
    if (
        snapshot.purchase_id != target.purchase_id
        or snapshot.buff_order_id != target.buff_order_id
        or snapshot.account_id != target.account_id
        or snapshot.recipient_steam_id != target.recipient_steam_id
    ):
        raise CanaryReceiveTickError("canary_store_identity_mismatch")
    if snapshot.delivery_status in {
        DeliveryStatus.BLOCKED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.REFUNDED,
    }:
        raise CanaryReceiveTickError("canary_store_terminal_unsafe")
    if snapshot.delivery_status is DeliveryStatus.RECEIVED:
        if (
            snapshot.pending_receipt is not False
            or type(snapshot.assetid) is not str
            or not snapshot.assetid
            or snapshot.assetid.strip() != snapshot.assetid
        ):
            raise CanaryReceiveTickError("canary_store_receipt_invalid")
    elif snapshot.pending_receipt is not True or snapshot.assetid is not None:
        raise CanaryReceiveTickError("canary_store_state_invalid")
    return stored


def _recoverable_by_order(integration) -> dict[str, StoredDelivery]:
    try:
        rows = tuple(integration.list_recoverable())
    except Exception as exc:
        raise CanaryReceiveTickError("canary_store_read_failed") from exc
    result: dict[str, StoredDelivery] = {}
    for item in rows:
        if type(item) is not StoredDelivery:
            raise CanaryReceiveTickError("canary_store_row_invalid")
        order_id = item.snapshot.buff_order_id
        if (
            type(order_id) is not str
            or not order_id
            or order_id.strip() != order_id
            or order_id in result
        ):
            raise CanaryReceiveTickError("canary_store_identity_invalid")
        result[order_id] = item
    return result


def _validate_authority_set(
    integration,
    target: CanaryReceiveTarget,
) -> StoredDelivery:
    try:
        stored = integration.get_by_purchase_id(target.purchase_id)
    except Exception as exc:
        raise CanaryReceiveTickError("canary_store_read_failed") from exc
    stored = _validate_target_stored(stored, target)
    recoverable = _recoverable_by_order(integration)
    if set(recoverable) - {target.buff_order_id}:
        raise CanaryReceiveTickError("canary_store_target_not_exclusive")

    if stored.snapshot.delivery_status is DeliveryStatus.RECEIVED:
        if target.buff_order_id in recoverable:
            raise CanaryReceiveTickError("canary_store_target_not_exclusive")
    elif recoverable != {target.buff_order_id: stored}:
        raise CanaryReceiveTickError("canary_store_target_not_exclusive")
    return stored


def _runtime_for_target(config: Mapping[str, object], host_purchases):
    from .runtime_lifecycle import get_effective_runtime_state

    try:
        runtime_state = get_effective_runtime_state(
            config=config,
            purchases=host_purchases,
        )
    except Exception as exc:
        raise CanaryReceiveTickError("canary_runtime_inspection_failed") from exc
    if runtime_state.mode not in {
        AutoOfferRuntimeMode.ON,
        AutoOfferRuntimeMode.DRAINING,
    }:
        raise CanaryReceiveTickError(
            f"canary_runtime_{runtime_state.mode.value}"
        )
    if runtime_state.active_delivery_count != 1:
        raise CanaryReceiveTickError("canary_runtime_target_count_invalid")
    return runtime_state


def run_receive_owned_canary_tick(
    target: CanaryReceiveTarget,
    host_purchases: Sequence[Mapping[str, object]],
    *,
    cursor: str | None = None,
) -> CanaryReceiveTickResult:
    """Run one exact canary delivery tick with resources owned by this thread."""

    if type(target) is not CanaryReceiveTarget:
        raise CanaryReceiveTickError("canary_target_invalid")
    if not isinstance(host_purchases, Sequence) or isinstance(
        host_purchases,
        (str, bytes, bytearray),
    ):
        raise CanaryReceiveTickError("canary_host_snapshot_invalid")

    from app.config_loader import (
        get_buff_credentials,
        load_app_config_validated,
    )
    from app.services.buff_checkout_guard import get_unresolved_checkout
    from app.services.buff_client import create_buff_client_from_config
    from app.state import get_state

    try:
        if get_unresolved_checkout() is not None:
            raise CanaryReceiveTickError("canary_checkout_unresolved")
    except CanaryReceiveTickError:
        raise
    except Exception as exc:
        raise CanaryReceiveTickError("canary_checkout_probe_failed") from exc

    try:
        config = load_app_config_validated()
    except Exception as exc:
        raise CanaryReceiveTickError("canary_config_load_failed") from exc
    if not isinstance(config, Mapping):
        raise CanaryReceiveTickError("canary_config_invalid")

    runtime_state = _runtime_for_target(config, host_purchases)

    try:
        credentials = get_buff_credentials() or {}
    except Exception as exc:
        raise CanaryReceiveTickError("canary_buff_credentials_read_failed") from exc
    if not isinstance(credentials, Mapping) or not credentials.get("cookies"):
        raise CanaryReceiveTickError("canary_buff_credentials_missing")

    buff_client = None
    integration = None
    result: CanaryReceiveTickResult | None = None
    primary_error: Exception | None = None
    try:
        try:
            buff_client = create_buff_client_from_config(
                dict(credentials),
                dict(config),
            )
        except Exception as exc:
            raise CanaryReceiveTickError(
                "canary_buff_client_build_failed"
            ) from exc

        state = get_state()
        try:
            integration = build_host_auto_offer_integration(
                config=config,
                buff_client=buff_client,
                complete_purchase_receipt_by_id=state.complete_purchase_receipt_by_id,
                delete_refund_cleanup_purchase=state.delete_refund_cleanup_purchase,
                runtime_state=runtime_state,
            )
        except HostAutoOfferIntegrationError as exc:
            raise CanaryReceiveTickError(
                "canary_receive_integration_build_failed"
            ) from exc
        except Exception as exc:
            raise CanaryReceiveTickError(
                "canary_receive_integration_build_failed"
            ) from exc
        if integration is None:
            raise CanaryReceiveTickError(
                "canary_receive_integration_missing"
            )
        if (
            integration.account_id != target.account_id
            or integration.recipient_steam_id != target.recipient_steam_id
        ):
            raise CanaryReceiveTickError("canary_runtime_identity_mismatch")

        before = _validate_authority_set(integration, target)
        if before.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            outcome = DeliveryTickOutcome(
                AutoOfferResult.RESULT_UNKNOWN,
                target.buff_order_id,
                (target.buff_order_id,),
            )
            result = CanaryReceiveTickResult(
                outcome=outcome,
                stored=before,
                runtime_mode=runtime_state.mode,
                reason="canary_result_unknown",
            )
        else:
            try:
                outcome = integration.run_delivery_tick(
                    list(host_purchases),
                    cursor=cursor,
                )
            except Exception as exc:
                raise CanaryReceiveTickError(
                    "canary_delivery_execution_failed"
                ) from exc
            if type(outcome) is not DeliveryTickOutcome:
                raise CanaryReceiveTickError(
                    "canary_delivery_tick_outcome_invalid"
                )

            after = _validate_authority_set(integration, target)
            reason = None
            if outcome.result is AutoOfferResult.RESULT_UNKNOWN:
                reason = "canary_result_unknown"
            elif outcome.result is AutoOfferResult.BLOCKED:
                reason = "canary_platform_or_state_blocked"
            result = CanaryReceiveTickResult(
                outcome=outcome,
                stored=after,
                runtime_mode=runtime_state.mode,
                reason=reason,
            )
    except Exception as exc:
        primary_error = exc
    finally:
        close_error: Exception | None = None
        if integration is not None:
            try:
                integration.close()
            except Exception as exc:
                close_error = CanaryReceiveTickError(
                    "canary_receive_integration_close_failed"
                )
                close_error.__cause__ = exc
        if buff_client is not None:
            try:
                close = getattr(buff_client, "close", None)
                if not callable(close):
                    raise TypeError("buff_client_close_missing")
                close()
            except Exception as exc:
                if close_error is None:
                    close_error = CanaryReceiveTickError(
                        "canary_buff_client_close_failed"
                    )
                    close_error.__cause__ = exc
        if primary_error is None and close_error is not None:
            primary_error = close_error

    if primary_error is not None:
        raise primary_error
    if result is None:
        raise CanaryReceiveTickError("canary_receive_tick_missing_result")
    return result


__all__ = [
    "CanaryReceiveTarget",
    "CanaryReceiveTickError",
    "CanaryReceiveTickResult",
    "run_receive_owned_canary_tick",
]
