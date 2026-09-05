"""Thin Host-owned fresh-canary target fence.

The canary is deliberately process-local and ephemeral. It admits one quiet
runtime, captures exactly one committed Host/BUFF purchase, and then keeps the
same normal Host Auto Offer integration for direction discovery and delivery.

It does not build a second Store/Coordinator/Steam runtime. Store state remains
the delivery authority; the normal Host integration keeps its existing exact
write barriers, durable OFFER_ATTEMPTED semantics, and RESULT_UNKNOWN handling.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .canary_authority import canary_metadata_present
from .contracts import AutoOfferResult, DeliveryStatus, TERMINAL_DELIVERY_STATUSES
from .store import StoredDelivery


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"


class CanaryTakeoverError(RuntimeError):
    """A fail-closed prepare or target-fence failure."""


class CanaryTakeoverPhase(str, Enum):
    IDLE = "IDLE"
    PREPARED = "PREPARED"
    TARGET_CAPTURED = "TARGET_CAPTURED"
    # Compatibility name. No second owner integration is created anymore.
    OWNER_ACTIVE = "OWNER_ACTIVE"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class CanaryTakeoverStatus:
    phase: CanaryTakeoverPhase
    expected_counterparty_steam_id: str | None = None
    expected_is_our_offer: bool | None = None
    host_db_id: int | None = None
    buff_order_id: str | None = None
    purchase_id: str | None = None
    account_id: str | None = None
    recipient_steam_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"phase": self.phase.value}
        for key in (
            "expected_counterparty_steam_id",
            "expected_is_our_offer",
            "host_db_id",
            "buff_order_id",
            "purchase_id",
            "account_id",
            "recipient_steam_id",
            "reason",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


def _canonical_steam_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise CanaryTakeoverError("invalid_expected_counterparty_steam_id")
    if not value.isascii() or not value.isdecimal() or value[0] == "0":
        raise CanaryTakeoverError("invalid_expected_counterparty_steam_id")
    if int(value) <= 0 or str(int(value)) != value:
        raise CanaryTakeoverError("invalid_expected_counterparty_steam_id")
    return value


def _default_host_purchases() -> list[Mapping[str, object]]:
    from app.state import get_purchases

    return get_purchases()


def _default_store_rows():
    from .store import AutoOfferStore

    return AutoOfferStore.inspect_existing(_STORE_PATH)


def _default_checkout() -> object | None:
    from app.services.buff_checkout_guard import get_unresolved_checkout

    return get_unresolved_checkout()


class CanaryTakeover:
    """One in-process prepare/capture target fence over the normal integration."""

    def __init__(
        self,
        *,
        host_purchases_provider: Callable[
            [], Sequence[Mapping[str, object]]
        ]
        | None = None,
        store_rows_provider: Callable[[], Sequence[object]] | None = None,
        checkout_provider: Callable[[], object | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._host_purchases_provider = (
            host_purchases_provider or _default_host_purchases
        )
        self._store_rows_provider = store_rows_provider or _default_store_rows
        self._checkout_provider = checkout_provider or _default_checkout
        # Retained for constructor compatibility with existing tests/callers.
        self._clock = clock
        self._lock = threading.RLock()
        self._phase = CanaryTakeoverPhase.IDLE
        self._expected_counterparty: str | None = None
        self._expected_is_our_offer: bool | None = None
        self._target: dict[str, object] = {}
        self._reason: str | None = None
        self._active_integration = None
        self._captured_integration = None

    def status(self) -> CanaryTakeoverStatus:
        with self._lock:
            return CanaryTakeoverStatus(
                phase=self._phase,
                expected_counterparty_steam_id=self._expected_counterparty,
                expected_is_our_offer=self._expected_is_our_offer,
                host_db_id=self._target.get("host_db_id"),
                buff_order_id=self._target.get("buff_order_id"),
                purchase_id=self._target.get("purchase_id"),
                account_id=self._target.get("account_id"),
                recipient_steam_id=self._target.get("recipient_steam_id"),
                reason=self._reason,
            )

    @property
    def phase(self) -> CanaryTakeoverPhase:
        with self._lock:
            return self._phase

    @property
    def is_prepared(self) -> bool:
        return self.phase is CanaryTakeoverPhase.PREPARED

    @property
    def owner_active(self) -> bool:
        """Compatibility property: the one target fence is active.

        OWNER_ACTIVE no longer means a second integration owns delivery.
        """

        with self._lock:
            return (
                self._phase is CanaryTakeoverPhase.OWNER_ACTIVE
                and self._active_integration is not None
            )

    @property
    def receive_blocked(self) -> bool:
        return self.phase is not CanaryTakeoverPhase.IDLE

    @property
    def purchase_blocked(self) -> bool:
        return self.phase in {
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
            CanaryTakeoverPhase.COMPLETE,
            CanaryTakeoverPhase.ABORTED,
        }

    def active_integration(self):
        with self._lock:
            return self._active_integration

    def prepare(
        self,
        *,
        host_purchases: Sequence[Mapping[str, object]] | None = None,
        store_rows: Sequence[object] | None = None,
    ) -> CanaryTakeoverStatus:
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.IDLE:
                raise CanaryTakeoverError("canary_takeover_not_idle")
            try:
                if canary_metadata_present():
                    raise CanaryTakeoverError("canary_authority_fenced")
                purchases = list(
                    self._host_purchases_provider()
                    if host_purchases is None
                    else host_purchases
                )
                if any(
                    not isinstance(item, Mapping)
                    or item.get("pending_receipt") is True
                    for item in purchases
                ):
                    raise CanaryTakeoverError("canary_prepare_host_not_quiet")
                if self._checkout_provider() is not None:
                    raise CanaryTakeoverError(
                        "canary_prepare_checkout_unresolved"
                    )
                rows = list(
                    self._store_rows_provider()
                    if store_rows is None
                    else store_rows
                )
                for stored in rows:
                    if type(stored) is not StoredDelivery:
                        raise CanaryTakeoverError(
                            "canary_prepare_store_invalid"
                        )
                    if (
                        stored.snapshot.delivery_status
                        not in TERMINAL_DELIVERY_STATUSES
                    ):
                        raise CanaryTakeoverError(
                            "canary_prepare_store_not_quiet"
                        )
            except CanaryTakeoverError:
                raise
            except Exception as exc:
                raise CanaryTakeoverError(
                    "canary_prepare_snapshot_failed"
                ) from exc

            self._expected_counterparty = None
            self._expected_is_our_offer = None
            self._target = {}
            self._reason = None
            self._active_integration = None
            self._captured_integration = None
            self._phase = CanaryTakeoverPhase.PREPARED
            return self.status()

    def cancel(self) -> CanaryTakeoverStatus:
        with self._lock:
            if self._phase is CanaryTakeoverPhase.IDLE:
                return self.status()
            if self._phase is not CanaryTakeoverPhase.PREPARED:
                raise CanaryTakeoverError("canary_takeover_cancel_unsafe")
            self._clear_locked()
            return self.status()

    def _clear_locked(self) -> None:
        self._phase = CanaryTakeoverPhase.IDLE
        self._expected_counterparty = None
        self._expected_is_our_offer = None
        self._target = {}
        self._reason = None
        self._active_integration = None
        self._captured_integration = None

    @staticmethod
    def _close_quietly(integration) -> None:
        if integration is None:
            return
        close = getattr(integration, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _abort_locked(self, reason: str) -> None:
        integration = self._active_integration or self._captured_integration
        self._active_integration = None
        self._captured_integration = None
        self._reason = reason
        self._phase = CanaryTakeoverPhase.ABORTED
        self._close_quietly(integration)

    def _validate_captured_host_locked(
        self, host_purchases: object
    ) -> list[Mapping[str, object]]:
        order_id = self._target.get("buff_order_id")
        db_id = self._target.get("host_db_id")
        if not isinstance(host_purchases, Sequence) or isinstance(
            host_purchases, (str, bytes)
        ):
            raise CanaryTakeoverError("canary_host_snapshot_invalid")
        pending = [
            item
            for item in host_purchases
            if isinstance(item, Mapping)
            and item.get("pending_receipt") is True
            and item.get("assetid") in (None, "")
        ]
        matches = [
            item
            for item in pending
            if item.get("buff_order_id") == order_id
            and item.get("_db_id") == db_id
        ]
        if len(pending) != 1 or len(matches) != 1:
            raise CanaryTakeoverError(
                "canary_host_target_not_exclusive"
            )
        return list(host_purchases)

    def _validate_active_host_locked(
        self, host_purchases: object
    ) -> list[Mapping[str, object]]:
        """Allow the target row to disappear only after Host receipt closure.

        Any remaining pending Host row must still be the exact captured target.
        If the target has just been closed, the normal integration remains the
        authority that must prove Store/Host completion on this tick.
        """

        order_id = self._target.get("buff_order_id")
        db_id = self._target.get("host_db_id")
        if not isinstance(host_purchases, Sequence) or isinstance(
            host_purchases, (str, bytes)
        ):
            raise CanaryTakeoverError("canary_host_snapshot_invalid")
        pending = [
            item
            for item in host_purchases
            if isinstance(item, Mapping)
            and item.get("pending_receipt") is True
            and item.get("assetid") in (None, "")
        ]
        if not pending:
            return list(host_purchases)
        matches = [
            item
            for item in pending
            if item.get("buff_order_id") == order_id
            and item.get("_db_id") == db_id
        ]
        if len(pending) != 1 or len(matches) != 1:
            raise CanaryTakeoverError(
                "canary_host_target_not_exclusive"
            )
        return list(host_purchases)

    def _target_identity_locked(
        self,
    ) -> tuple[str, str, int, str, str]:
        order_id = self._target.get("buff_order_id")
        purchase_id = self._target.get("purchase_id")
        db_id = self._target.get("host_db_id")
        account_id = self._target.get("account_id")
        recipient = self._target.get("recipient_steam_id")
        if (
            type(order_id) is not str
            or type(purchase_id) is not str
            or type(db_id) is not int
            or type(account_id) is not str
            or type(recipient) is not str
        ):
            raise CanaryTakeoverError("canary_target_invalid")
        return order_id, purchase_id, db_id, account_id, recipient

    def _activate_if_direction_bound_locked(
        self,
        host_purchases: list[Mapping[str, object]],
        *,
        run_direction_read: bool,
    ) -> bool:
        """Bind the target fence after the normal state machine proves direction.

        The same normal integration is retained. No permit snapshot, Store
        reopen, Steam session rebuild, Coordinator rebuild, or owner runtime
        migration occurs here.
        """

        normal = self._captured_integration
        if normal is None:
            raise CanaryTakeoverError("canary_capture_context_missing")
        (
            _order_id,
            purchase_id,
            _db_id,
            _account_id,
            recipient,
        ) = self._target_identity_locked()

        stored = normal.get_by_purchase_id(purchase_id)
        if type(stored) is not StoredDelivery:
            raise CanaryTakeoverError("canary_store_target_missing")

        if (
            stored.snapshot.delivery_status
            is DeliveryStatus.PENDING_DIRECTION
            and run_direction_read
        ):
            outcome = normal.run_delivery_tick(
                host_purchases,
                cursor=None,
            )
            result = getattr(outcome, "result", None)
            if result in {
                AutoOfferResult.BLOCKED,
                AutoOfferResult.RESULT_UNKNOWN,
            }:
                raise CanaryTakeoverError(
                    "canary_direction_read_blocked"
                )
            if result not in {
                AutoOfferResult.WAITING,
                AutoOfferResult.COMPLETE,
            }:
                raise CanaryTakeoverError(
                    "canary_direction_read_invalid"
                )
            stored = normal.get_by_purchase_id(purchase_id)
            if type(stored) is not StoredDelivery:
                raise CanaryTakeoverError(
                    "canary_store_target_missing"
                )

        snapshot = stored.snapshot
        if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
            return False
        if snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER:
            raise CanaryTakeoverError("canary_direction_state_invalid")

        from .contracts import DeliveryMode

        if snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.counterparty_steam_id is not None:
                raise CanaryTakeoverError(
                    "canary_buyer_counterparty_premature"
                )
            counterparty = None
            is_our_offer = True
        elif snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER:
            counterparty = _canonical_steam_id(
                snapshot.counterparty_steam_id
            )
            if counterparty == _canonical_steam_id(recipient):
                raise CanaryTakeoverError(
                    "canary_counterparty_invalid"
                )
            is_our_offer = False
        else:
            raise CanaryTakeoverError("canary_direction_state_invalid")

        recoverable = tuple(normal.list_recoverable())
        if recoverable != (stored,):
            raise CanaryTakeoverError(
                "canary_store_target_not_exclusive"
            )
        if self._checkout_provider() is not None:
            raise CanaryTakeoverError("canary_checkout_unresolved")

        self._expected_counterparty = counterparty
        self._expected_is_our_offer = is_our_offer
        self._active_integration = normal
        self._captured_integration = None
        self._phase = CanaryTakeoverPhase.OWNER_ACTIVE
        return True

    def capture_committed_purchases(
        self,
        purchases: Sequence[Mapping[str, object]],
        *,
        normal_integration,
        build_canary_integration: Callable[[object], object] | None = None,
        reconcile_checkout: Callable[[], object] | None = None,
    ) -> CanaryTakeoverStatus:
        """Capture one committed target and keep its normal integration.

        ``build_canary_integration`` is accepted only as a compatibility
        argument for the existing pipeline call shape. It is intentionally
        never invoked.
        """

        if not isinstance(purchases, Sequence) or isinstance(
            purchases, (str, bytes)
        ):
            raise CanaryTakeoverError(
                "canary_commit_snapshot_invalid"
            )
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.PREPARED:
                raise CanaryTakeoverError(
                    "canary_takeover_not_prepared"
                )
            self._phase = CanaryTakeoverPhase.TARGET_CAPTURED
            try:
                if len(purchases) != 1:
                    raise CanaryTakeoverError(
                        "canary_multiple_committed_purchases"
                    )
                committed = purchases[0]
                if not isinstance(committed, Mapping):
                    raise CanaryTakeoverError(
                        "canary_commit_snapshot_invalid"
                    )
                order_id = committed.get("buff_order_id")
                if (
                    type(order_id) is not str
                    or not order_id
                    or order_id.strip() != order_id
                ):
                    raise CanaryTakeoverError("canary_target_invalid")

                host_purchases = list(
                    self._host_purchases_provider()
                )
                pending = [
                    item
                    for item in host_purchases
                    if isinstance(item, Mapping)
                    and item.get("pending_receipt") is True
                    and item.get("assetid") in (None, "")
                ]
                matches = [
                    item
                    for item in pending
                    if item.get("buff_order_id") == order_id
                ]
                if len(pending) != 1 or len(matches) != 1:
                    raise CanaryTakeoverError(
                        "canary_host_target_not_exclusive"
                    )
                target = matches[0]
                db_id = target.get("_db_id")
                if type(db_id) is not int or db_id <= 0:
                    raise CanaryTakeoverError(
                        "canary_host_target_invalid"
                    )

                unresolved = self._checkout_provider()
                if (
                    unresolved is not None
                    and reconcile_checkout is not None
                ):
                    reconcile_checkout()
                    unresolved = self._checkout_provider()
                if unresolved is not None:
                    raise CanaryTakeoverError(
                        "canary_checkout_unresolved"
                    )

                account_id = normal_integration.account_id
                recipient = normal_integration.recipient_steam_id
                self._target = {
                    "host_db_id": db_id,
                    "buff_order_id": order_id,
                    "purchase_id": f"buff:{order_id}",
                    "account_id": account_id,
                    "recipient_steam_id": recipient,
                }
                self._captured_integration = normal_integration

                # The compatibility builder must never be called.
                _ = build_canary_integration

                self._activate_if_direction_bound_locked(
                    host_purchases,
                    run_direction_read=True,
                )
                return self.status()
            except CanaryTakeoverError as exc:
                self._abort_locked(
                    str(exc) or type(exc).__name__
                )
                raise
            except Exception as exc:
                self._abort_locked(type(exc).__name__)
                raise CanaryTakeoverError(
                    "canary_takeover_activation_failed"
                ) from exc

    def run_capture_binding_tick(self, host_purchases: object):
        """Run one normal read step while the single target remains fenced."""

        from .host_integration import DeliveryTickOutcome

        with self._lock:
            if self._phase is not CanaryTakeoverPhase.TARGET_CAPTURED:
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    None,
                    (),
                )
            order_id = self._target.get("buff_order_id")
            try:
                current_host = self._validate_captured_host_locked(
                    host_purchases
                )
                self._activate_if_direction_bound_locked(
                    current_host,
                    run_direction_read=True,
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.WAITING,
                    order_id
                    if isinstance(order_id, str)
                    else None,
                    (order_id,)
                    if isinstance(order_id, str)
                    else (),
                )
            except CanaryTakeoverError as exc:
                self._abort_locked(
                    str(exc) or type(exc).__name__
                )
            except Exception as exc:
                self._abort_locked(type(exc).__name__)
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

    def run_owner_tick(self, host_purchases: object):
        """Continue the exact target through the same normal integration."""

        from .host_integration import DeliveryTickOutcome

        with self._lock:
            active = self._active_integration
            order_id = self._target.get("buff_order_id")
            if (
                self._phase is not CanaryTakeoverPhase.OWNER_ACTIVE
                or active is None
            ):
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    None,
                    (),
                )
            try:
                current_host = self._validate_active_host_locked(
                    host_purchases
                )
            except CanaryTakeoverError as exc:
                self._abort_locked(
                    str(exc) or type(exc).__name__
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id
                    if isinstance(order_id, str)
                    else None,
                    (order_id,)
                    if isinstance(order_id, str)
                    else (),
                )

        try:
            outcome = active.run_delivery_tick(
                current_host,
                cursor=None,
            )
        except Exception as exc:
            with self._lock:
                self._abort_locked(type(exc).__name__)
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

        if type(outcome) is not DeliveryTickOutcome:
            with self._lock:
                self._abort_locked(
                    "canary_delivery_tick_outcome_invalid"
                )
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

        if outcome.result is AutoOfferResult.COMPLETE:
            with self._lock:
                try:
                    active.close()
                except Exception:
                    self._abort_locked(
                        "canary_target_close_failed"
                    )
                    return DeliveryTickOutcome(
                        AutoOfferResult.BLOCKED,
                        order_id
                        if isinstance(order_id, str)
                        else None,
                        (order_id,)
                        if isinstance(order_id, str)
                        else (),
                    )
                self._phase = CanaryTakeoverPhase.COMPLETE
                self._active_integration = None

        return outcome


class CanaryTakeoverIntegration:
    """Thin wrapper that fences purchase count while retaining normal delivery."""

    def __init__(
        self,
        controller: CanaryTakeover,
        normal_integration,
    ) -> None:
        self._controller = controller
        self._normal = normal_integration
        self._normal_closed = False

    @property
    def account_id(self) -> str:
        return self._normal.account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._normal.recipient_steam_id

    @property
    def is_canary(self) -> bool:
        return self._controller.owner_active

    @property
    def canary_completed(self) -> bool:
        return (
            self._controller.phase
            is CanaryTakeoverPhase.COMPLETE
        )

    @property
    def registration_enabled(self) -> bool:
        return bool(
            getattr(
                self._normal,
                "registration_enabled",
                True,
            )
        )

    def register_committed_purchase(
        self,
        purchase: Mapping[str, object],
    ):
        if self._controller.purchase_blocked:
            raise CanaryTakeoverError(
                "canary_second_purchase_forbidden"
            )
        return self._normal.register_committed_purchase(purchase)

    def capture_committed_purchases(
        self,
        purchases: Sequence[Mapping[str, object]],
        *,
        build_canary_integration: Callable[[object], object] | None = None,
        reconcile_checkout: Callable[[], object] | None = None,
    ) -> CanaryTakeoverStatus:
        return self._controller.capture_committed_purchases(
            purchases,
            normal_integration=self._normal,
            build_canary_integration=build_canary_integration,
            reconcile_checkout=reconcile_checkout,
        )

    def next_purchase_result(
        self,
        host_purchases: object,
    ) -> AutoOfferResult:
        if self._controller.purchase_blocked:
            return AutoOfferResult.BLOCKED
        return self._normal.next_purchase_result(
            host_purchases
        )

    def run_delivery_tick(
        self,
        host_purchases: object,
        *,
        cursor: str | None = None,
    ):
        if self._controller.owner_active:
            return self._controller.run_owner_tick(
                host_purchases
            )
        if (
            self._controller.phase
            is CanaryTakeoverPhase.TARGET_CAPTURED
        ):
            return self._controller.run_capture_binding_tick(
                host_purchases
            )
        return self._normal.run_delivery_tick(
            host_purchases,
            cursor=cursor,
        )

    def close(self) -> None:
        # Once a target is captured, the same normal integration is retained by
        # the takeover and is closed only on COMPLETE/ABORT.
        if self._controller.owner_active or self._controller.phase in {
            CanaryTakeoverPhase.COMPLETE,
            CanaryTakeoverPhase.ABORTED,
            CanaryTakeoverPhase.TARGET_CAPTURED,
        }:
            return
        if not self._normal_closed:
            self._normal_closed = True
            self._normal.close()


_TAKEOVER_LOCK = threading.Lock()
_TAKEOVER: CanaryTakeover | None = None


def get_canary_takeover() -> CanaryTakeover:
    global _TAKEOVER
    with _TAKEOVER_LOCK:
        if _TAKEOVER is None:
            _TAKEOVER = CanaryTakeover()
        return _TAKEOVER


__all__ = [
    "CanaryTakeover",
    "CanaryTakeoverError",
    "CanaryTakeoverIntegration",
    "CanaryTakeoverPhase",
    "CanaryTakeoverStatus",
    "get_canary_takeover",
]
