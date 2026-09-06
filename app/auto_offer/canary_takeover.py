"""Thin Host-owned fresh-canary target fence.

The canary owns only admission, one exact target identity, a one-purchase fence,
and a small secret-free crash marker. It never carries a Store, Coordinator,
Steam session, BUFF client, or Host integration across threads.

Purchase registration/capture happens in the buy-pipeline thread. Once a target
is captured, that thread closes its ordinary Host integration. The existing Host
receive worker remains the sole delivery scheduler; each canary delivery tick
provisions a fresh ordinary Host integration in the receive-worker thread.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .canary_authority import canary_metadata_present
from .contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliveryStatus,
    TERMINAL_DELIVERY_STATUSES,
)
from .store import StoredDelivery


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"
_TARGET_FENCE_PATH = (
    Path(__file__).resolve().parents[2]
    / ".aetherswap"
    / "canary-takeover.json"
)
_TARGET_FENCE_VERSION = 1


class CanaryTakeoverError(RuntimeError):
    """A fail-closed prepare, target-fence, or receive handoff failure."""


class CanaryTakeoverPhase(str, Enum):
    IDLE = "IDLE"
    PREPARED = "PREPARED"
    TARGET_CAPTURED = "TARGET_CAPTURED"
    # Compatibility label: this means direction-bound exact target fence.
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


def _exact_text(value: object, reason: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 32 for character in value)
    ):
        raise CanaryTakeoverError(reason)
    return value


def _canonical_steam_id(value: object) -> str:
    text = _exact_text(value, "invalid_expected_counterparty_steam_id")
    if not text.isascii() or not text.isdecimal() or text[0] == "0":
        raise CanaryTakeoverError("invalid_expected_counterparty_steam_id")
    if int(text) <= 0 or str(int(text)) != text:
        raise CanaryTakeoverError("invalid_expected_counterparty_steam_id")
    return text


def _default_host_purchases() -> list[Mapping[str, object]]:
    from app.state import get_purchases

    return get_purchases()


def _default_store_rows():
    from .store import AutoOfferStore

    return AutoOfferStore.inspect_existing(_STORE_PATH)


def _default_checkout() -> object | None:
    from app.services.buff_checkout_guard import get_unresolved_checkout

    return get_unresolved_checkout()


def _target_record(target: Mapping[str, object]) -> dict[str, object]:
    return {
        "version": _TARGET_FENCE_VERSION,
        "phase": "target",
        "host_db_id": target["host_db_id"],
        "buff_order_id": target["buff_order_id"],
        "purchase_id": target["purchase_id"],
        "account_id": target["account_id"],
        "recipient_steam_id": target["recipient_steam_id"],
    }


class CanaryTakeover:
    """One-process exact-target fence with receive-thread-owned delivery."""

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
        target_fence_path: str | Path | None = None,
    ) -> None:
        self._host_purchases_provider = (
            host_purchases_provider or _default_host_purchases
        )
        self._store_rows_provider = store_rows_provider or _default_store_rows
        self._checkout_provider = checkout_provider or _default_checkout
        # Retained for constructor compatibility and deterministic tests.
        self._clock = clock
        self._target_fence_path = (
            None if target_fence_path is None else Path(target_fence_path)
        )
        self._lock = threading.RLock()
        self._phase = CanaryTakeoverPhase.IDLE
        self._expected_counterparty: str | None = None
        self._expected_is_our_offer: bool | None = None
        self._target: dict[str, object] = {}
        self._reason: str | None = None
        self._load_restart_fence()

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
        """Compatibility surface used by the buy-pipeline canary wrapper.

        Once the one target is captured, later pipeline-start attempts must
        remain wrapped so they stop at the existing target fence rather than
        falling back to the normal multi-purchase path. COMPLETE is included so
        a second start reports the completed canary instead of buying again.
        """

        return self.phase in {
            CanaryTakeoverPhase.PREPARED,
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
            CanaryTakeoverPhase.COMPLETE,
        }

    @property
    def owner_active(self) -> bool:
        """Compatibility property for a direction-bound target fence."""

        return self.phase is CanaryTakeoverPhase.OWNER_ACTIVE

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
        """Compatibility seam: TASK-100 deliberately retains no integration."""

        return None

    def _load_restart_fence(self) -> None:
        path = self._target_fence_path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            self._phase = CanaryTakeoverPhase.ABORTED
            self._reason = "canary_target_fence_unreadable"
            return
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if (
            not isinstance(data, dict)
            or data.get("version") != _TARGET_FENCE_VERSION
            or data.get("phase") not in {"prepared", "target"}
        ):
            self._phase = CanaryTakeoverPhase.ABORTED
            self._reason = "canary_target_fence_invalid"
            return
        if data["phase"] == "target":
            try:
                db_id = data.get("host_db_id")
                order_id = _exact_text(
                    data.get("buff_order_id"),
                    "canary_target_fence_invalid",
                )
                purchase_id = _exact_text(
                    data.get("purchase_id"),
                    "canary_target_fence_invalid",
                )
                account_id = _exact_text(
                    data.get("account_id"),
                    "canary_target_fence_invalid",
                )
                recipient = _canonical_steam_id(
                    data.get("recipient_steam_id")
                )
                if (
                    type(db_id) is not int
                    or db_id <= 0
                    or purchase_id != f"buff:{order_id}"
                ):
                    raise CanaryTakeoverError(
                        "canary_target_fence_invalid"
                    )
                self._target = {
                    "host_db_id": db_id,
                    "buff_order_id": order_id,
                    "purchase_id": purchase_id,
                    "account_id": account_id,
                    "recipient_steam_id": recipient,
                }
            except CanaryTakeoverError:
                self._phase = CanaryTakeoverPhase.ABORTED
                self._reason = "canary_target_fence_invalid"
                return
        self._phase = CanaryTakeoverPhase.ABORTED
        self._reason = "canary_restart_recovery_required"

    def _write_fence_locked(self, payload: Mapping[str, object]) -> None:
        path = self._target_fence_path
        if path is None:
            return
        temporary_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=str(path.parent),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    dict(payload),
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            temporary_name = None
        except Exception as exc:
            raise CanaryTakeoverError(
                "canary_target_fence_write_failed"
            ) from exc
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def _remove_fence_locked(self) -> None:
        path = self._target_fence_path
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CanaryTakeoverError(
                "canary_target_fence_retire_failed"
            ) from exc

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
                    raise CanaryTakeoverError(
                        "canary_prepare_host_not_quiet"
                    )
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
                self._write_fence_locked(
                    {
                        "version": _TARGET_FENCE_VERSION,
                        "phase": "prepared",
                    }
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
            self._phase = CanaryTakeoverPhase.PREPARED
            return self.status()

    def cancel(self) -> CanaryTakeoverStatus:
        with self._lock:
            if self._phase is CanaryTakeoverPhase.IDLE:
                return self.status()
            if self._phase is not CanaryTakeoverPhase.PREPARED:
                raise CanaryTakeoverError("canary_takeover_cancel_unsafe")
            self._remove_fence_locked()
            self._clear_locked()
            return self.status()

    def retire_restart_fence(self) -> CanaryTakeoverStatus:
        """Explicit local recovery seam; never called automatically."""

        with self._lock:
            if (
                self._phase is not CanaryTakeoverPhase.ABORTED
                or self._reason
                not in {
                    "canary_restart_recovery_required",
                    "canary_target_fence_invalid",
                    "canary_target_fence_unreadable",
                }
            ):
                raise CanaryTakeoverError(
                    "canary_restart_fence_retire_unsafe"
                )
            self._remove_fence_locked()
            self._clear_locked()
            return self.status()

    def _clear_locked(self) -> None:
        self._phase = CanaryTakeoverPhase.IDLE
        self._expected_counterparty = None
        self._expected_is_our_offer = None
        self._target = {}
        self._reason = None

    def _abort_locked(self, reason: str) -> None:
        self._reason = _exact_text(
            reason or "canary_takeover_aborted",
            "canary_abort_reason_invalid",
        )
        self._phase = CanaryTakeoverPhase.ABORTED

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
            or db_id <= 0
            or type(account_id) is not str
            or type(recipient) is not str
            or purchase_id != f"buff:{order_id}"
        ):
            raise CanaryTakeoverError("canary_target_invalid")
        return order_id, purchase_id, db_id, account_id, recipient

    def _target_for_receive_locked(self):
        from .canary_receive import CanaryReceiveTarget

        order_id, purchase_id, db_id, account_id, recipient = (
            self._target_identity_locked()
        )
        return CanaryReceiveTarget(
            host_db_id=db_id,
            buff_order_id=order_id,
            purchase_id=purchase_id,
            account_id=account_id,
            recipient_steam_id=recipient,
        )

    def _validate_host_locked(
        self,
        host_purchases: object,
        *,
        allow_missing_target: bool,
    ) -> tuple[list[Mapping[str, object]], bool]:
        order_id, _purchase_id, db_id, _account_id, _recipient = (
            self._target_identity_locked()
        )
        if not isinstance(host_purchases, Sequence) or isinstance(
            host_purchases,
            (str, bytes, bytearray),
        ):
            raise CanaryTakeoverError("canary_host_snapshot_invalid")
        rows = list(host_purchases)
        if any(not isinstance(item, Mapping) for item in rows):
            raise CanaryTakeoverError("canary_host_snapshot_invalid")
        pending: list[Mapping[str, object]] = []
        for item in rows:
            if item.get("pending_receipt") is not True:
                continue
            if item.get("assetid") not in (None, ""):
                raise CanaryTakeoverError("canary_host_pending_invalid")
            pending.append(item)
        if not pending:
            if allow_missing_target:
                return rows, False
            raise CanaryTakeoverError(
                "canary_host_target_not_exclusive"
            )
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
        return rows, True

    def _validate_stored_identity_locked(
        self,
        stored: object,
    ) -> StoredDelivery:
        order_id, purchase_id, _db_id, account_id, recipient = (
            self._target_identity_locked()
        )
        if type(stored) is not StoredDelivery:
            raise CanaryTakeoverError("canary_store_target_missing")
        snapshot = stored.snapshot
        if (
            snapshot.purchase_id != purchase_id
            or snapshot.buff_order_id != order_id
            or snapshot.account_id != account_id
            or snapshot.recipient_steam_id != recipient
        ):
            raise CanaryTakeoverError(
                "canary_store_identity_mismatch"
            )
        return stored

    def _bind_direction_locked(self, stored: StoredDelivery) -> bool:
        stored = self._validate_stored_identity_locked(stored)
        snapshot = stored.snapshot
        if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
            self._phase = CanaryTakeoverPhase.TARGET_CAPTURED
            return False
        if snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER:
            raise CanaryTakeoverError(
                "canary_direction_state_invalid"
            )
        _order_id, _purchase_id, _db_id, _account_id, recipient = (
            self._target_identity_locked()
        )
        if snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER:
            if snapshot.counterparty_steam_id is not None:
                raise CanaryTakeoverError(
                    "canary_buyer_counterparty_premature"
                )
            self._expected_counterparty = None
            self._expected_is_our_offer = True
        elif snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER:
            counterparty = _canonical_steam_id(
                snapshot.counterparty_steam_id
            )
            if counterparty == _canonical_steam_id(recipient):
                raise CanaryTakeoverError(
                    "canary_counterparty_invalid"
                )
            self._expected_counterparty = counterparty
            self._expected_is_our_offer = False
        else:
            raise CanaryTakeoverError(
                "canary_direction_state_invalid"
            )
        self._phase = CanaryTakeoverPhase.OWNER_ACTIVE
        return True

    def _validate_bound_direction_locked(
        self,
        stored: StoredDelivery,
    ) -> None:
        stored = self._validate_stored_identity_locked(stored)
        snapshot = stored.snapshot
        _order_id, _purchase_id, _db_id, _account_id, recipient = (
            self._target_identity_locked()
        )
        if self._expected_is_our_offer is True:
            if snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER:
                raise CanaryTakeoverError(
                    "canary_direction_identity_drift"
                )
            if snapshot.counterparty_steam_id is not None:
                counterparty = _canonical_steam_id(
                    snapshot.counterparty_steam_id
                )
                if counterparty == _canonical_steam_id(recipient):
                    raise CanaryTakeoverError(
                        "canary_counterparty_invalid"
                    )
        elif self._expected_is_our_offer is False:
            if snapshot.delivery_mode is not DeliveryMode.SELLER_SENDS_OFFER:
                raise CanaryTakeoverError(
                    "canary_direction_identity_drift"
                )
            counterparty = _canonical_steam_id(
                snapshot.counterparty_steam_id
            )
            if counterparty != self._expected_counterparty:
                raise CanaryTakeoverError(
                    "canary_counterparty_identity_drift"
                )
        else:
            raise CanaryTakeoverError(
                "canary_direction_identity_missing"
            )

    def _terminal_received_without_host_locked(self) -> bool:
        order_id, _purchase_id, _db_id, _account_id, _recipient = (
            self._target_identity_locked()
        )
        try:
            rows = list(self._store_rows_provider())
        except Exception as exc:
            raise CanaryTakeoverError(
                "canary_store_snapshot_failed"
            ) from exc
        target: StoredDelivery | None = None
        for item in rows:
            if type(item) is not StoredDelivery:
                raise CanaryTakeoverError(
                    "canary_store_row_invalid"
                )
            if (
                item.snapshot.delivery_status
                not in TERMINAL_DELIVERY_STATUSES
            ):
                raise CanaryTakeoverError(
                    "canary_store_target_not_exclusive"
                )
            if item.snapshot.buff_order_id == order_id:
                if target is not None:
                    raise CanaryTakeoverError(
                        "canary_store_target_not_exclusive"
                    )
                target = item
        target = self._validate_stored_identity_locked(target)
        snapshot = target.snapshot
        return (
            snapshot.delivery_status is DeliveryStatus.RECEIVED
            and snapshot.pending_receipt is False
            and type(snapshot.assetid) is str
            and bool(snapshot.assetid)
            and snapshot.assetid.strip() == snapshot.assetid
        )

    def _complete_locked(self) -> None:
        self._remove_fence_locked()
        self._phase = CanaryTakeoverPhase.COMPLETE
        self._reason = None

    @staticmethod
    def _receive_failure_reason(exc: Exception) -> str:
        from .canary_receive import CanaryReceiveTickError

        if isinstance(exc, CanaryReceiveTickError):
            reason = str(exc)
            if reason and reason.strip() == reason and not any(
                ord(character) < 32 for character in reason
            ):
                return reason
        return f"canary_receive_unexpected_{type(exc).__name__}"

    def capture_committed_purchases(
        self,
        purchases: Sequence[Mapping[str, object]],
        *,
        normal_integration,
        build_canary_integration: Callable[[object], object] | None = None,
        reconcile_checkout: Callable[[], object] | None = None,
    ) -> CanaryTakeoverStatus:
        """Capture one committed target; never retain the pipeline integration."""

        if not isinstance(purchases, Sequence) or isinstance(
            purchases,
            (str, bytes, bytearray),
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
                order_id = _exact_text(
                    committed.get("buff_order_id"),
                    "canary_target_invalid",
                )

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
                target_host = matches[0]
                db_id = target_host.get("_db_id")
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

                account_id = _exact_text(
                    normal_integration.account_id,
                    "canary_account_invalid",
                )
                recipient = _canonical_steam_id(
                    normal_integration.recipient_steam_id
                )
                self._target = {
                    "host_db_id": db_id,
                    "buff_order_id": order_id,
                    "purchase_id": f"buff:{order_id}",
                    "account_id": account_id,
                    "recipient_steam_id": recipient,
                }

                stored = normal_integration.get_by_purchase_id(
                    f"buff:{order_id}"
                )
                stored = self._validate_stored_identity_locked(stored)
                recoverable = tuple(
                    normal_integration.list_recoverable()
                )
                if recoverable != (stored,):
                    raise CanaryTakeoverError(
                        "canary_store_target_not_exclusive"
                    )
                if stored.snapshot.delivery_status in {
                    DeliveryStatus.RESULT_UNKNOWN,
                    DeliveryStatus.BLOCKED,
                    DeliveryStatus.CANCELLED,
                    DeliveryStatus.REFUNDED,
                    DeliveryStatus.RECEIVED,
                }:
                    raise CanaryTakeoverError(
                        "canary_capture_store_state_invalid"
                    )

                # Compatibility argument only; no second integration exists.
                _ = build_canary_integration

                # Upgrade the durable PREPARED crash fence to exact target
                # identity before any receive-thread delivery step is possible.
                self._write_fence_locked(_target_record(self._target))
                self._bind_direction_locked(stored)
                return self.status()
            except CanaryTakeoverError as exc:
                self._abort_locked(
                    str(exc) or type(exc).__name__
                )
                raise
            except Exception as exc:
                self._abort_locked(
                    "canary_takeover_activation_failed"
                )
                raise CanaryTakeoverError(
                    "canary_takeover_activation_failed"
                ) from exc

    def _run_receive_tick(self, host_rows):
        from .canary_receive import run_receive_owned_canary_tick

        with self._lock:
            target = self._target_for_receive_locked()
        return run_receive_owned_canary_tick(
            target,
            host_rows,
            cursor=None,
        )

    def run_capture_binding_tick(self, host_purchases: object):
        """Let the receive worker perform exactly one direction-binding step."""

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
                current_host, _present = self._validate_host_locked(
                    host_purchases,
                    allow_missing_target=False,
                )
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc))
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )

        try:
            tick = self._run_receive_tick(current_host)
        except Exception as exc:
            reason = self._receive_failure_reason(exc)
            with self._lock:
                self._abort_locked(reason)
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

        outcome = tick.outcome
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.TARGET_CAPTURED:
                self._abort_locked(
                    "canary_phase_changed_during_receive_tick"
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
            if outcome.result is AutoOfferResult.RESULT_UNKNOWN:
                self._abort_locked(
                    tick.reason or "canary_result_unknown"
                )
                return outcome
            if outcome.result is AutoOfferResult.BLOCKED:
                self._abort_locked(
                    tick.reason or "canary_delivery_blocked"
                )
                return outcome
            if outcome.result is not AutoOfferResult.WAITING:
                self._abort_locked(
                    "canary_direction_tick_invalid_result"
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
            try:
                self._bind_direction_locked(tick.stored)
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc))
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
        return outcome

    def run_owner_tick(self, host_purchases: object):
        """Continue the exact target using receive-thread-owned resources only."""

        from .host_integration import DeliveryTickOutcome

        with self._lock:
            order_id = self._target.get("buff_order_id")
            if self._phase is not CanaryTakeoverPhase.OWNER_ACTIVE:
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    None,
                    (),
                )
            try:
                current_host, target_present = self._validate_host_locked(
                    host_purchases,
                    allow_missing_target=True,
                )
                if not target_present:
                    if not self._terminal_received_without_host_locked():
                        self._abort_locked(
                            "canary_terminal_receipt_not_proven"
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
                        self._complete_locked()
                    except CanaryTakeoverError as exc:
                        self._abort_locked(str(exc))
                        return DeliveryTickOutcome(
                            AutoOfferResult.BLOCKED,
                            order_id
                            if isinstance(order_id, str)
                            else None,
                            (order_id,)
                            if isinstance(order_id, str)
                            else (),
                        )
                    return DeliveryTickOutcome(
                        AutoOfferResult.COMPLETE,
                        None,
                        (order_id,)
                        if isinstance(order_id, str)
                        else (),
                    )
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc))
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )

        try:
            tick = self._run_receive_tick(current_host)
        except Exception as exc:
            reason = self._receive_failure_reason(exc)
            with self._lock:
                self._abort_locked(reason)
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if isinstance(order_id, str) else None,
                (order_id,) if isinstance(order_id, str) else (),
            )

        outcome = tick.outcome
        with self._lock:
            if self._phase is not CanaryTakeoverPhase.OWNER_ACTIVE:
                self._abort_locked(
                    "canary_phase_changed_during_receive_tick"
                )
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
            try:
                self._validate_bound_direction_locked(tick.stored)
            except CanaryTakeoverError as exc:
                self._abort_locked(str(exc))
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
            if outcome.result is AutoOfferResult.RESULT_UNKNOWN:
                self._abort_locked(
                    tick.reason or "canary_result_unknown"
                )
            elif outcome.result is AutoOfferResult.BLOCKED:
                self._abort_locked(
                    tick.reason or "canary_delivery_blocked"
                )
            elif outcome.result is AutoOfferResult.COMPLETE:
                # Host row was present at the start of this tick; exact receipt
                # closure is re-proven from fresh Host/Store snapshots next tick.
                return DeliveryTickOutcome(
                    AutoOfferResult.WAITING,
                    order_id if isinstance(order_id, str) else None,
                    (order_id,) if isinstance(order_id, str) else (),
                )
        return outcome


class CanaryTakeoverIntegration:
    """Pipeline-thread wrapper that owns only purchase registration/capture."""

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
        return self._controller.phase in {
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
        }

    @property
    def canary_completed(self) -> bool:
        return self._controller.phase is CanaryTakeoverPhase.COMPLETE

    @property
    def purchase_fence_active(self) -> bool:
        return self._controller.phase in {
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
        }

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
        phase = self._controller.phase
        if phase in {
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
        }:
            return AutoOfferResult.WAITING
        if phase is CanaryTakeoverPhase.COMPLETE:
            return AutoOfferResult.COMPLETE
        if phase is CanaryTakeoverPhase.ABORTED:
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
        from .host_integration import DeliveryTickOutcome

        if self._controller.phase in {
            CanaryTakeoverPhase.TARGET_CAPTURED,
            CanaryTakeoverPhase.OWNER_ACTIVE,
        }:
            order_id = self._controller.status().buff_order_id
            return DeliveryTickOutcome(
                AutoOfferResult.WAITING,
                order_id,
                (order_id,) if order_id is not None else (),
            )
        return self._normal.run_delivery_tick(
            host_purchases,
            cursor=cursor,
        )

    def close(self) -> None:
        # The pipeline-created Store/Coordinator/Steam session is always closed
        # in its origin thread, including after target capture.
        if not self._normal_closed:
            self._normal_closed = True
            self._normal.close()


_TAKEOVER_LOCK = threading.Lock()
_TAKEOVER: CanaryTakeover | None = None


def get_canary_takeover() -> CanaryTakeover:
    global _TAKEOVER
    with _TAKEOVER_LOCK:
        if _TAKEOVER is None:
            _TAKEOVER = CanaryTakeover(
                target_fence_path=_TARGET_FENCE_PATH,
            )
        return _TAKEOVER


__all__ = [
    "CanaryTakeover",
    "CanaryTakeoverError",
    "CanaryTakeoverIntegration",
    "CanaryTakeoverPhase",
    "CanaryTakeoverStatus",
    "get_canary_takeover",
]
