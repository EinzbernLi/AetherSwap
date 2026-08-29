"""Host-owned lifecycle for explicitly enabled native Auto Offer execution.

The host integration remains default-off. When enabled it owns one local Store,
one Coordinator registry, one Steam session, and the reviewed exact SEND and
mobile-confirmation adapters. Platform work is synchronous and bounded; no
worker, retry loop, scheduler, or background Auto Offer executor is created.

TASK-036 adds an optional canary-only exact-target authority. TASK-042 keeps
that canary path separate while normal admission and worker progression use
distinct bounded façades.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import requests

from app.accounts import get_account, get_current_id
from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
)
from app.auto_offer.canary_authority import (
    CanaryAuthority,
    CanaryAuthorityError,
    CanaryPermit,
    CanaryWriteTarget,
    get_canary_authority,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from app.auto_offer.runtime_mode import AutoOfferRuntimeMode, AutoOfferRuntimeState
from app.auto_offer.coordinator import (
    AcceptOfferStepResult,
    ConfirmationAuthorityReadResult,
    DeliveryCoordinator,
    ReadOnlyStepResult,
    SellerAcceptAuthorityReadResult,
)
from app.auto_offer.platform_accept import SteamIncomingOfferAcceptAdapter
from app.auto_offer.platform_confirmation import SteamTradeOfferConfirmationAdapter
from app.auto_offer.platform_readonly import (
    BuffReadOnlyAdapter,
    SteamCompletedTradeReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)
from app.auto_offer.platform_write import BuffBuyerSendOfferAdapter
from app.auto_offer.steam_confirmation_transport import (
    SteamTradeOfferConfirmationTransport,
)
from app.auto_offer.steam_accept_transport import (
    SteamIncomingOfferAcceptTransport,
)
from app.auto_offer.steam_readonly_transport import (
    SteamCompletedTradeHttpReader,
    SteamTradeOfferHttpReader,
)
from app.auto_offer.store import AutoOfferStore, StoredDelivery
from app.config_loader import get_steam_credentials
from app.services.buff_checkout_guard import get_unresolved_checkout


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"
_TIMEOUT_SECONDS = 15.0
MAX_DELIVERY_ORDERS_PER_TICK = 8
_MAX_CANARY_RECOVERY_STEPS_PER_DELIVERY = 6
_RECOVERY_ONLY_CAPABILITIES = frozenset(
    {
        PlatformCapability.READ_OFFER_STATE,
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        PlatformCapability.READ_STEAM_COMPLETED_TRADE,
    }
)
_RECOVERY_ONLY_CONTINUATION_STATUSES = frozenset(
    {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RECEIVED,
    }
)


@dataclass(frozen=True)
class DeliveryTickOutcome:
    result: AutoOfferResult
    next_cursor: str | None
    visited_order_ids: tuple[str, ...]


class HostAutoOfferIntegrationError(RuntimeError):
    """Raised when the host cannot safely execute the Auto Offer lifecycle."""


def is_auto_offer_enabled(config: Mapping[str, object] | None) -> bool:
    """Return the validated, exact boolean host feature flag."""

    if not isinstance(config, Mapping):
        return False
    section = config.get("auto_offer")
    if section is None:
        return False
    if not isinstance(section, Mapping):
        raise HostAutoOfferIntegrationError("invalid_auto_offer_config")
    enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise HostAutoOfferIntegrationError("auto_offer_enabled_must_be_bool")
    return enabled


def _canonical_steam_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise HostAutoOfferIntegrationError("steam_id_invalid")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise HostAutoOfferIntegrationError("steam_id_invalid")
    return value


def _exact_current_account() -> tuple[str, str]:
    current_id = get_current_id()
    if not isinstance(current_id, str) or not current_id or current_id.strip() != current_id:
        raise HostAutoOfferIntegrationError("current_account_id_invalid")

    account = get_account(current_id)
    if not isinstance(account, Mapping) or account.get("id") != current_id:
        raise HostAutoOfferIntegrationError("current_account_not_found")
    account_steam_id = account.get("steam_id")
    if (
        not isinstance(account_steam_id, str)
        or not account_steam_id
        or account_steam_id.strip() != account_steam_id
    ):
        raise HostAutoOfferIntegrationError("current_account_steam_id_invalid")
    return current_id, account_steam_id


def _exact_order_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    return value


def _exact_assetid(value: object) -> str | None:
    if type(value) is not str or not value or value.strip() != value:
        return None
    return value


def _exact_db_id(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def _exact_goods_id(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def _steam_credentials_for_expected(expected_steam_id: str) -> Mapping[str, object]:
    expected = _canonical_steam_id(expected_steam_id)
    credentials = get_steam_credentials()
    if not isinstance(credentials, Mapping):
        raise HostAutoOfferIntegrationError("steam_credentials_invalid")
    actual = _canonical_steam_id(credentials.get("steam_id"))
    if actual != expected:
        raise HostAutoOfferIntegrationError("steam_identity_mismatch")
    return credentials


def _steam_cookie_for_expected(expected_steam_id: str) -> str:
    credentials = _steam_credentials_for_expected(expected_steam_id)
    cookies = credentials.get("cookies")
    if type(cookies) is not str or not cookies:
        raise HostAutoOfferIntegrationError("steam_cookie_required")
    return cookies


def _steam_confirmation_credentials_for_expected(
    expected_steam_id: str,
) -> tuple[str, str]:
    credentials = _steam_credentials_for_expected(expected_steam_id)
    cookies = credentials.get("cookies")
    identity_secret = credentials.get("identity_secret")
    if type(cookies) is not str or not cookies:
        raise HostAutoOfferIntegrationError("steam_cookie_required")
    if (
        type(identity_secret) is not str
        or not identity_secret
        or identity_secret.strip() != identity_secret
        or any(character.isspace() for character in identity_secret)
    ):
        raise HostAutoOfferIntegrationError("steam_identity_secret_required")
    return cookies, identity_secret


def _safe_close_store(store: AutoOfferStore | None) -> bool:
    if store is None:
        return True
    try:
        store.close()
        return True
    except Exception:
        return False


def _safe_close_session(session: object | None) -> bool:
    if session is None:
        return True
    try:
        close = getattr(session, "close")
        if not callable(close):
            return False
        close()
        return True
    except Exception:
        return False


def _persisted_recovery_policy(delivery: StoredDelivery) -> str:
    """Return read/confirm/wait/block without recreating first-send authority."""

    if type(delivery) is not StoredDelivery:
        return "block"
    snapshot = delivery.snapshot
    status = snapshot.delivery_status
    mode = snapshot.delivery_mode
    if status is DeliveryStatus.PENDING_DIRECTION:
        return "read"
    if status is DeliveryStatus.AWAITING_OFFER:
        if mode is DeliveryMode.SELLER_SENDS_OFFER:
            return "read"
        if mode is DeliveryMode.BUYER_SENDS_OFFER:
            return "wait"
        return "block"
    if status is DeliveryStatus.OFFER_ATTEMPTED:
        return "read" if mode is DeliveryMode.BUYER_SENDS_OFFER else "block"
    if status is DeliveryStatus.RESULT_UNKNOWN:
        return "read" if mode is DeliveryMode.BUYER_SENDS_OFFER else "block"
    if status is DeliveryStatus.OFFER_RECEIVED:
        return "read" if mode is DeliveryMode.SELLER_SENDS_OFFER else "block"
    if status is DeliveryStatus.OFFER_SENT:
        return "read" if mode is DeliveryMode.BUYER_SENDS_OFFER else "block"
    if status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED:
        return "confirm" if mode is DeliveryMode.BUYER_SENDS_OFFER else "block"
    if status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED:
        return "read" if mode is DeliveryMode.BUYER_SENDS_OFFER else "block"
    if status is DeliveryStatus.OFFER_CONFIRMED:
        if mode in {
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryMode.BUYER_SENDS_OFFER,
        }:
            return "read"
        return "block"
    if status is DeliveryStatus.AWAITING_INVENTORY:
        if mode in {
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryMode.BUYER_SENDS_OFFER,
        }:
            return "read"
        return "block"
    return "block"


def _preflight_host_pending_by_order(
    host_purchases: object,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(host_purchases, list):
        raise HostAutoOfferIntegrationError("invalid_host_purchases")
    pending: dict[str, Mapping[str, object]] = {}
    for purchase in host_purchases:
        if not isinstance(purchase, Mapping):
            raise HostAutoOfferIntegrationError("invalid_host_purchase")
        if purchase.get("pending_receipt") is not True:
            continue
        if purchase.get("assetid") not in (None, ""):
            raise HostAutoOfferIntegrationError("pending_host_has_asset")
        order_id = _exact_order_id(purchase.get("buff_order_id"))
        db_id = _exact_db_id(purchase.get("_db_id"))
        if order_id is None or db_id is None or order_id in pending:
            raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
        pending[order_id] = purchase
    return pending


def _cleanup_expected_present(
    host_purchases: object,
    buff_order_id: str,
) -> bool:
    if not isinstance(host_purchases, list):
        raise HostAutoOfferIntegrationError("invalid_host_purchases")
    matches: list[Mapping[str, object]] = []
    for purchase in host_purchases:
        if not isinstance(purchase, Mapping):
            raise HostAutoOfferIntegrationError("invalid_host_purchase")
        value = purchase.get("buff_order_id")
        if value not in (None, "") and _exact_order_id(value) is None:
            raise HostAutoOfferIntegrationError("invalid_host_order_identity")
        if value == buff_order_id:
            matches.append(purchase)
    if not matches:
        return False
    if len(matches) != 1:
        raise HostAutoOfferIntegrationError("duplicate_host_order_identity")
    purchase = matches[0]
    if (
        _exact_db_id(purchase.get("_db_id")) is None
        or purchase.get("pending_receipt") is not True
        or purchase.get("assetid") not in (None, "")
    ):
        raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
    return True


def _preflight_validate_stored(
    stored: object,
    *,
    order_id: str,
    account_id: str,
    recipient_steam_id: str,
) -> StoredDelivery:
    if type(stored) is not StoredDelivery:
        raise HostAutoOfferIntegrationError("invalid_store_delivery")
    snapshot = stored.snapshot
    if (
        snapshot.purchase_id != f"buff:{order_id}"
        or snapshot.buff_order_id != order_id
        or snapshot.account_id != account_id
        or snapshot.recipient_steam_id != recipient_steam_id
    ):
        raise HostAutoOfferIntegrationError("canary_store_target_invalid")
    if snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
        raise HostAutoOfferIntegrationError("canary_result_unknown_ineligible")
    if snapshot.delivery_status is DeliveryStatus.RECEIVED:
        if snapshot.pending_receipt is not False or _exact_assetid(snapshot.assetid) is None:
            raise HostAutoOfferIntegrationError("canary_store_target_invalid")
        return stored
    if (
        snapshot.pending_receipt is not True
        or snapshot.assetid is not None
        or snapshot.delivery_status in {
            DeliveryStatus.BLOCKED,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.REFUNDED,
        }
    ):
        raise HostAutoOfferIntegrationError("canary_store_target_invalid")
    return stored


def preflight_canary_permit(
    *,
    host_purchases: object,
    unresolved_checkout: object,
    recoverable_deliveries: Sequence[StoredDelivery],
    target_stored: StoredDelivery | None,
    target_db_id: int,
    target_buff_order_id: str,
    account_id: str,
    recipient_steam_id: str,
    expected_counterparty_steam_id: str,
    expected_is_our_offer: bool,
    permit_id: str,
    owner_nonce: str,
    created_at: float,
) -> CanaryPermit:
    """Build one permit from supplied local snapshots without performing a write.

    This function deliberately receives detached values instead of a Store,
    session, or platform client. It cannot dispatch, CAS, purchase, confirm,
    receive, list, or write back a receipt.
    """

    if unresolved_checkout is not None:
        raise HostAutoOfferIntegrationError("canary_checkout_unresolved")
    order_id = _exact_order_id(target_buff_order_id)
    db_id = _exact_db_id(target_db_id)
    if order_id is None or db_id is None:
        raise HostAutoOfferIntegrationError("canary_target_invalid")
    if type(account_id) is not str or not account_id or account_id.strip() != account_id:
        raise HostAutoOfferIntegrationError("canary_account_invalid")
    recipient = _canonical_steam_id(recipient_steam_id)
    counterparty = _canonical_steam_id(expected_counterparty_steam_id)
    if counterparty == recipient:
        raise HostAutoOfferIntegrationError("canary_counterparty_invalid")
    if type(expected_is_our_offer) is not bool:
        raise HostAutoOfferIntegrationError("canary_direction_invalid")

    pending = _preflight_host_pending_by_order(host_purchases)
    if set(pending) != {order_id} or pending[order_id].get("_db_id") != db_id:
        raise HostAutoOfferIntegrationError("canary_host_target_not_exclusive")

    if not isinstance(recoverable_deliveries, Sequence) or isinstance(
        recoverable_deliveries,
        (str, bytes, bytearray),
    ):
        raise HostAutoOfferIntegrationError("invalid_canary_recoverable_snapshot")
    recoverable: dict[str, StoredDelivery] = {}
    for item in recoverable_deliveries:
        if type(item) is not StoredDelivery:
            raise HostAutoOfferIntegrationError("invalid_store_delivery")
        item_order = _exact_order_id(item.snapshot.buff_order_id)
        if item_order is None or item_order in recoverable:
            raise HostAutoOfferIntegrationError("invalid_store_order_identity")
        recoverable[item_order] = item
    if set(recoverable) - {order_id}:
        raise HostAutoOfferIntegrationError("canary_unrelated_store_row")

    if target_stored is None:
        if recoverable:
            raise HostAutoOfferIntegrationError("canary_store_snapshot_mismatch")
        expected_present = False
        expected_revision = None
        expected_status = None
        expected_tradeoffer_id = None
    else:
        stored = _preflight_validate_stored(
            target_stored,
            order_id=order_id,
            account_id=account_id,
            recipient_steam_id=recipient,
        )
        if stored.snapshot.delivery_status is DeliveryStatus.RECEIVED:
            if recoverable:
                raise HostAutoOfferIntegrationError("canary_store_snapshot_mismatch")
        elif recoverable != {order_id: stored}:
            raise HostAutoOfferIntegrationError("canary_store_snapshot_mismatch")
        expected_present = True
        expected_revision = stored.revision
        expected_status = stored.snapshot.delivery_status.value
        expected_tradeoffer_id = stored.snapshot.steam_tradeoffer_id

    try:
        return CanaryPermit(
            permit_id=permit_id,
            owner_nonce=owner_nonce,
            host_db_id=db_id,
            buff_order_id=order_id,
            purchase_id=f"buff:{order_id}",
            account_id=account_id,
            recipient_steam_id=recipient,
            expected_counterparty_steam_id=counterparty,
            expected_is_our_offer=expected_is_our_offer,
            expected_host_order_ids=(order_id,),
            expected_store_present=expected_present,
            expected_store_revision=expected_revision,
            expected_store_status=expected_status,
            expected_store_tradeoffer_id=expected_tradeoffer_id,
            created_at=created_at,
        )
    except CanaryAuthorityError as exc:
        raise HostAutoOfferIntegrationError("canary_permit_invalid") from exc


class _BuffClientBuyerSendTransport:
    """Transport shape backed only by the public run-owned BUFF facade."""

    __slots__ = ("_send",)

    def __init__(self, buff_client) -> None:
        send = getattr(buff_client, "send_buyer_offer", None)
        if not callable(send):
            raise HostAutoOfferIntegrationError("buff_client_send_surface_required")
        self._send = send

    def send(
        self,
        *,
        steam_cookie_string: str,
        buff_order_id: str,
        steam_id: str,
        timeout_seconds: float,
    ) -> dict:
        return self._send(
            steam_cookie_string=steam_cookie_string,
            buff_order_id=buff_order_id,
            steam_id=steam_id,
            timeout_seconds=timeout_seconds,
        )


class _ActiveHostAutoOfferBridge:
    """One owned Store/Coordinator/session bundle for an enabled host run."""

    __slots__ = (
        "_store",
        "_coordinator",
        "_session",
        "_account_id",
        "_recipient_steam_id",
        "_closed",
    )

    def __init__(
        self,
        *,
        store: AutoOfferStore,
        coordinator: DeliveryCoordinator,
        session: object,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._session = session
        self._account_id = account_id
        self._recipient_steam_id = recipient_steam_id
        self._closed = False

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    def _require_open(self) -> None:
        if self._closed:
            raise HostAutoOfferIntegrationError("bridge_closed")

    def register_committed_purchase(
        self,
        record: Mapping[str, object],
    ) -> StoredDelivery | None:
        """Return a first-send token only for an atomic fresh Store insert."""

        self._require_open()
        if not isinstance(record, Mapping):
            raise HostAutoOfferIntegrationError("invalid_purchase_record")
        buff_order_id = record.get("buff_order_id")
        pending_receipt = record.get("pending_receipt")
        assetid = record.get("assetid")
        if (
            type(buff_order_id) is not str
            or not buff_order_id
            or buff_order_id.strip() != buff_order_id
        ):
            raise HostAutoOfferIntegrationError("invalid_buff_order_id")
        if pending_receipt is not True:
            raise HostAutoOfferIntegrationError("purchase_not_pending_receipt")
        if assetid not in (None, ""):
            raise HostAutoOfferIntegrationError("purchase_already_has_asset")

        snapshot = DeliverySnapshot(
            purchase_id=f"buff:{buff_order_id}",
            buff_order_id=buff_order_id,
            account_id=self._account_id,
            recipient_steam_id=self._recipient_steam_id,
            delivery_mode=None,
            delivery_status=DeliveryStatus.PENDING_DIRECTION,
            steam_tradeoffer_id=None,
            offer_attempted_at=None,
            offer_sent_at=None,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        )
        try:
            stored, created = self._store.ensure_initial_with_created(snapshot)
        except Exception:
            raise HostAutoOfferIntegrationError("purchase_registration_failed") from None
        return stored if created else None

    def list_recoverable(self) -> tuple[StoredDelivery, ...]:
        self._require_open()
        try:
            return tuple(self._store.list_recoverable())
        except Exception:
            raise HostAutoOfferIntegrationError("store_read_failed") from None

    def get_by_purchase_id(self, purchase_id: str) -> StoredDelivery | None:
        self._require_open()
        try:
            return self._store.get_by_purchase_id(purchase_id)
        except Exception:
            raise HostAutoOfferIntegrationError("store_read_failed") from None

    def step(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.step(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("auto_offer_step_failed") from None

    def read_send_authority(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.read_send_authority(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("send_authority_read_failed") from None

    def send_offer_with_authority(self, delivery: StoredDelivery, proof: object):
        self._require_open()
        try:
            return self._coordinator.send_offer_with_authority(delivery, proof)
        except Exception:
            raise HostAutoOfferIntegrationError("send_execution_failed") from None

    def recover_result_unknown_readonly(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.recover_result_unknown_readonly(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("result_unknown_recovery_failed") from None

    def read_confirmation_authority(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.read_confirmation_authority(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError(
                "confirmation_authority_read_failed"
            ) from None

    def confirm_offer_with_authority(self, delivery: StoredDelivery, proof: object):
        self._require_open()
        try:
            return self._coordinator.confirm_offer_with_authority(delivery, proof)
        except Exception:
            raise HostAutoOfferIntegrationError("confirmation_execution_failed") from None

    def recover_confirmation_result_unknown_readonly(
        self,
        delivery: StoredDelivery,
    ):
        self._require_open()
        try:
            return self._coordinator.recover_confirmation_result_unknown_readonly(
                delivery
            )
        except Exception:
            raise HostAutoOfferIntegrationError(
                "confirmation_result_unknown_recovery_failed"
            ) from None

    def read_seller_accept_authority(
        self,
        delivery: StoredDelivery,
        host_goods_id: int,
    ):
        self._require_open()
        try:
            return self._coordinator.read_seller_accept_authority(
                delivery,
                host_goods_id,
            )
        except Exception:
            raise HostAutoOfferIntegrationError(
                "seller_accept_authority_read_failed"
            ) from None

    def accept_offer_with_authority(
        self,
        delivery: StoredDelivery,
        proof: object,
    ):
        self._require_open()
        try:
            return self._coordinator.accept_offer_with_authority(
                delivery,
                proof,
            )
        except Exception:
            raise HostAutoOfferIntegrationError(
                "seller_accept_execution_failed"
            ) from None

    def recover_accept_result_unknown_readonly(
        self,
        delivery: StoredDelivery,
    ):
        self._require_open()
        try:
            return self._coordinator.recover_accept_result_unknown_readonly(
                delivery
            )
        except Exception:
            raise HostAutoOfferIntegrationError(
                "accept_result_unknown_recovery_failed"
            ) from None

    def complete_refund_cleanup(self, delivery: StoredDelivery) -> StoredDelivery:
        self._require_open()
        try:
            return self._coordinator.complete_refund_cleanup(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError(
                "refund_cleanup_store_advance_failed"
            ) from None

    def read_confirmation_state(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.read_confirmation_state(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("confirmation_read_failed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        store_ok = _safe_close_store(self._store)
        session_ok = _safe_close_session(self._session)
        if not store_ok or not session_ok:
            raise HostAutoOfferIntegrationError("bridge_close_failed")


def _coordinator_write_guard(
    authority: CanaryAuthority,
    owner_session: object | None,
    request: PlatformRequest,
):
    if request.capability is PlatformCapability.SEND_OFFER:
        action = "auto_offer_send"
    elif request.capability is PlatformCapability.ACCEPT_OFFER:
        action = "auto_offer_accept"
    elif request.capability is PlatformCapability.CONFIRM_OFFER:
        action = "auto_offer_confirm"
    else:
        raise HostAutoOfferIntegrationError("invalid_guarded_capability")
    target = CanaryWriteTarget(
        action=action,
        purchase_id=request.purchase_id,
        buff_order_id=request.buff_order_id,
        account_id=request.account_id,
        recipient_steam_id=request.recipient_steam_id,
    )
    if owner_session is not None:
        return owner_session.external_write_guard(target)
    return authority.external_write_guard(target)


def _build_active_host_auto_offer_bridge(
    *,
    buff_client,
    account_id: str,
    account_steam_id: str,
    store_path: str | Path = _STORE_PATH,
    canary_authority: CanaryAuthority | None = None,
    canary_owner_session: object | None = None,
    canary_permit: CanaryPermit | None = None,
) -> _ActiveHostAutoOfferBridge:
    """Build the one active read+SEND+confirmation Coordinator without I/O."""

    account = account_id
    if type(account) is not str or not account or account.strip() != account:
        raise HostAutoOfferIntegrationError("current_account_id_invalid")
    recipient = _canonical_steam_id(account_steam_id)
    authority = canary_authority or get_canary_authority()
    if type(authority) is not CanaryAuthority:
        raise HostAutoOfferIntegrationError("canary_authority_invalid")
    if canary_permit is not None:
        if type(canary_permit) is not CanaryPermit:
            raise HostAutoOfferIntegrationError("canary_permit_invalid")
        if (
            canary_permit.account_id != account
            or canary_permit.recipient_steam_id != recipient
        ):
            raise HostAutoOfferIntegrationError("canary_runtime_identity_mismatch")
    cookie_string, identity_secret = _steam_confirmation_credentials_for_expected(
        recipient
    )

    session = None
    store = None
    try:
        session = requests.Session()
        if getattr(session, "verify", None) is False:
            raise HostAutoOfferIntegrationError("steam_tls_verification_disabled")

        store = AutoOfferStore(store_path)
        store.initialize()

        timeout = (_TIMEOUT_SECONDS, _TIMEOUT_SECONDS)
        trade_offer_reader = SteamTradeOfferHttpReader(
            cookie_string,
            session=session,
            timeout=timeout,
        )
        completed_trade_reader = SteamCompletedTradeHttpReader(
            cookie_string,
            session=session,
            timeout=timeout,
        )
        confirmation_transport = SteamTradeOfferConfirmationTransport(
            cookie_string,
            identity_secret,
            session=session,
            timeout=timeout,
        )
        accept_transport = None
        if canary_permit is None:
            accept_transport = SteamIncomingOfferAcceptTransport(
                cookie_string,
                session=session,
            )
        if (
            trade_offer_reader.bound_account_steam_id != recipient
            or completed_trade_reader.bound_account_steam_id != recipient
            or confirmation_transport.bound_account_steam_id != recipient
            or (
                accept_transport is not None
                and accept_transport.bound_account_steam_id != recipient
            )
        ):
            raise HostAutoOfferIntegrationError("steam_identity_mismatch")

        buff_adapter = BuffReadOnlyAdapter(buff_client, account_id=account)
        trade_offer_adapter = SteamTradeOfferReadOnlyAdapter(
            trade_offer_reader,
            account_id=account,
            recipient_steam_id=recipient,
        )
        completed_trade_adapter = SteamCompletedTradeReadOnlyAdapter(
            completed_trade_reader,
            account_id=account,
            recipient_steam_id=recipient,
        )
        send_adapter = BuffBuyerSendOfferAdapter(
            _BuffClientBuyerSendTransport(buff_client),
            account_id=account,
            recipient_steam_id=recipient,
            steam_cookie_provider=lambda: _steam_cookie_for_expected(recipient),
        )
        confirmation_adapter = SteamTradeOfferConfirmationAdapter(
            confirmation_transport,
            account_id=account,
            recipient_steam_id=recipient,
        )
        adapters = {
            PlatformCapability.READ_DELIVERY_DIRECTION: buff_adapter,
            PlatformCapability.READ_OFFER_STATE: buff_adapter,
            PlatformCapability.READ_BUFF_ORDER_LIFECYCLE: buff_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: trade_offer_adapter,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE: completed_trade_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
            PlatformCapability.CONFIRM_OFFER: confirmation_adapter,
        }
        if accept_transport is not None:
            adapters[PlatformCapability.READ_SELLER_OFFER_ITEM] = buff_adapter
            adapters[PlatformCapability.ACCEPT_OFFER] = (
                SteamIncomingOfferAcceptAdapter(
                    accept_transport,
                    account_id=account,
                    recipient_steam_id=recipient,
                )
            )
        coordinator = DeliveryCoordinator(
            store,
            adapters,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_writes=True,
            allow_confirmation_writes=True,
            allow_accept_writes=accept_transport is not None,
            write_guard=lambda request: _coordinator_write_guard(
                authority,
                canary_owner_session,
                request,
            ),
            expected_trade_offer_counterparty_steam_id=(
                None
                if canary_permit is None
                else canary_permit.expected_counterparty_steam_id
            ),
            expected_trade_offer_is_our_offer=(
                None if canary_permit is None else canary_permit.expected_is_our_offer
            ),
        )
    except HostAutoOfferIntegrationError:
        _safe_close_store(store)
        _safe_close_session(session)
        raise
    except Exception:
        _safe_close_store(store)
        _safe_close_session(session)
        raise HostAutoOfferIntegrationError("auto_offer_bridge_build_failed") from None

    return _ActiveHostAutoOfferBridge(
        store=store,
        coordinator=coordinator,
        session=session,
        account_id=account,
        recipient_steam_id=recipient,
    )


class _RecoveryOnlyHostAutoOfferBridge:
    """Owned Store/Coordinator bundle with read capabilities only."""

    __slots__ = (
        "_store",
        "_coordinator",
        "_session",
        "_account_id",
        "_recipient_steam_id",
        "_closed",
    )

    def __init__(
        self,
        *,
        store: AutoOfferStore,
        coordinator: DeliveryCoordinator,
        session: object,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._session = session
        self._account_id = account_id
        self._recipient_steam_id = recipient_steam_id
        self._closed = False

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    @property
    def capabilities(self):
        return _RECOVERY_ONLY_CAPABILITIES

    def _require_open(self) -> None:
        if self._closed:
            raise HostAutoOfferIntegrationError("recovery_bridge_closed")

    def list_recoverable(self) -> tuple[StoredDelivery, ...]:
        self._require_open()
        try:
            return tuple(self._store.list_recoverable())
        except Exception:
            raise HostAutoOfferIntegrationError("recovery_store_read_failed") from None

    def get_by_purchase_id(self, purchase_id: str) -> StoredDelivery | None:
        self._require_open()
        try:
            return self._store.get_by_purchase_id(purchase_id)
        except Exception:
            raise HostAutoOfferIntegrationError("recovery_store_read_failed") from None

    def recover_result_unknown_readonly(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.recover_result_unknown_readonly(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError(
                "recovery_result_unknown_read_failed"
            ) from None

    def step(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.step(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("recovery_read_step_failed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        store_ok = _safe_close_store(self._store)
        session_ok = _safe_close_session(self._session)
        if not store_ok or not session_ok:
            raise HostAutoOfferIntegrationError("recovery_bridge_close_failed")


def _build_recovery_only_host_auto_offer_bridge(
    *,
    buff_client,
    account_id: str,
    account_steam_id: str,
    steam_cookie_string: str,
    store_path: str | Path = _STORE_PATH,
) -> _RecoveryOnlyHostAutoOfferBridge:
    """Build the explicit maintenance bridge without any platform writer."""

    if type(account_id) is not str or not account_id or account_id.strip() != account_id:
        raise HostAutoOfferIntegrationError("current_account_id_invalid")
    recipient = _canonical_steam_id(account_steam_id)
    if type(steam_cookie_string) is not str or not steam_cookie_string:
        raise HostAutoOfferIntegrationError("steam_cookie_required")

    session = None
    store = None
    try:
        session = requests.Session()
        if getattr(session, "verify", None) is False:
            raise HostAutoOfferIntegrationError("steam_tls_verification_disabled")

        store = AutoOfferStore(store_path)
        store.initialize_existing()

        timeout = (_TIMEOUT_SECONDS, _TIMEOUT_SECONDS)
        trade_offer_reader = SteamTradeOfferHttpReader(
            steam_cookie_string,
            session=session,
            timeout=timeout,
        )
        completed_trade_reader = SteamCompletedTradeHttpReader(
            steam_cookie_string,
            session=session,
            timeout=timeout,
        )
        if (
            trade_offer_reader.bound_account_steam_id != recipient
            or completed_trade_reader.bound_account_steam_id != recipient
        ):
            raise HostAutoOfferIntegrationError("steam_identity_mismatch")

        buff_adapter = BuffReadOnlyAdapter(buff_client, account_id=account_id)
        trade_offer_adapter = SteamTradeOfferReadOnlyAdapter(
            trade_offer_reader,
            account_id=account_id,
            recipient_steam_id=recipient,
        )
        completed_trade_adapter = SteamCompletedTradeReadOnlyAdapter(
            completed_trade_reader,
            account_id=account_id,
            recipient_steam_id=recipient,
        )
        adapters = {
            PlatformCapability.READ_OFFER_STATE: buff_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: trade_offer_adapter,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE: completed_trade_adapter,
        }
        if frozenset(adapters) != _RECOVERY_ONLY_CAPABILITIES:
            raise HostAutoOfferIntegrationError("recovery_capability_registry_invalid")

        coordinator = DeliveryCoordinator(
            store,
            adapters,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_writes=False,
            allow_confirmation_writes=False,
            allow_accept_writes=False,
        )
    except HostAutoOfferIntegrationError:
        _safe_close_store(store)
        _safe_close_session(session)
        raise
    except Exception:
        _safe_close_store(store)
        _safe_close_session(session)
        raise HostAutoOfferIntegrationError(
            "recovery_only_bridge_build_failed"
        ) from None

    return _RecoveryOnlyHostAutoOfferBridge(
        store=store,
        coordinator=coordinator,
        session=session,
        account_id=account_id,
        recipient_steam_id=recipient,
    )


class HostRecoveryOnlyMaintenance:
    """Host-owned, one-target, read-only recovery maintenance façade."""

    __slots__ = (
        "_bridge",
        "_complete_purchase_receipt_by_id",
        "_target_order_id",
        "_target_host_db_id",
        "_receipt_completed",
        "_closed",
    )

    def __init__(
        self,
        bridge,
        *,
        complete_purchase_receipt_by_id=None,
    ) -> None:
        try:
            account_id = bridge.account_id
            recipient_steam_id = bridge.recipient_steam_id
            capabilities = bridge.capabilities
        except Exception as exc:
            raise HostAutoOfferIntegrationError(
                "recovery_bridge_invalid"
            ) from exc
        if (
            type(account_id) is not str
            or not account_id
            or account_id.strip() != account_id
            or type(recipient_steam_id) is not str
            or not recipient_steam_id
            or frozenset(capabilities) != _RECOVERY_ONLY_CAPABILITIES
        ):
            raise HostAutoOfferIntegrationError("recovery_bridge_invalid")
        self._bridge = bridge
        self._complete_purchase_receipt_by_id = complete_purchase_receipt_by_id
        self._target_order_id: str | None = None
        self._target_host_db_id: int | None = None
        self._receipt_completed = False
        self._closed = False

    @property
    def account_id(self) -> str:
        return self._bridge.account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._bridge.recipient_steam_id

    @property
    def capabilities(self):
        return _RECOVERY_ONLY_CAPABILITIES

    def _require_open(self) -> None:
        if self._closed:
            raise HostAutoOfferIntegrationError("recovery_maintenance_closed")

    def _host_rows_for_order(
        self,
        host_purchases: object,
        order_id: str,
    ) -> list[Mapping[str, object]]:
        if not isinstance(host_purchases, list):
            raise HostAutoOfferIntegrationError("invalid_host_purchases")
        matches: list[Mapping[str, object]] = []
        for purchase in host_purchases:
            if not isinstance(purchase, Mapping):
                raise HostAutoOfferIntegrationError("invalid_host_purchase")
            value = purchase.get("buff_order_id")
            if value not in (None, "") and _exact_order_id(value) is None:
                raise HostAutoOfferIntegrationError("invalid_host_order_identity")
            if value == order_id:
                matches.append(purchase)
        return matches

    def _validate_optional_host_identity(
        self,
        purchase: Mapping[str, object],
    ) -> None:
        for field, expected in (
            ("account_id", self.account_id),
            ("recipient_steam_id", self.recipient_steam_id),
            ("steam_id", self.recipient_steam_id),
            ("buyer_steamid", self.recipient_steam_id),
            ("buyer_steam_id", self.recipient_steam_id),
        ):
            if field in purchase and purchase.get(field) != expected:
                raise HostAutoOfferIntegrationError("host_store_identity_mismatch")

    def _pending_host_target(
        self,
        host_purchases: object,
    ) -> tuple[str, Mapping[str, object]]:
        pending = _preflight_host_pending_by_order(host_purchases)
        if len(pending) != 1:
            raise HostAutoOfferIntegrationError("maintenance_host_target_not_exclusive")
        order_id, purchase = next(iter(pending.items()))
        matches = self._host_rows_for_order(host_purchases, order_id)
        if len(matches) != 1 or matches[0] is not purchase:
            raise HostAutoOfferIntegrationError("duplicate_host_order_identity")
        if _exact_db_id(purchase.get("_db_id")) is None:
            raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
        self._validate_optional_host_identity(purchase)
        return order_id, purchase

    def _receipt_host_target(
        self,
        host_purchases: object,
    ) -> tuple[str, Mapping[str, object]]:
        order_id = self._target_order_id
        if order_id is None:
            raise HostAutoOfferIntegrationError("maintenance_target_required")
        pending = _preflight_host_pending_by_order(host_purchases)
        if any(item != order_id for item in pending):
            raise HostAutoOfferIntegrationError("maintenance_host_target_not_exclusive")
        matches = self._host_rows_for_order(host_purchases, order_id)
        if len(matches) != 1:
            raise HostAutoOfferIntegrationError("duplicate_host_order_identity")
        purchase = matches[0]
        if _exact_db_id(purchase.get("_db_id")) is None:
            raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
        self._validate_optional_host_identity(purchase)
        return order_id, purchase

    def _validate_store_identity(
        self,
        stored: object,
        order_id: str,
    ) -> StoredDelivery:
        if type(stored) is not StoredDelivery:
            raise HostAutoOfferIntegrationError("invalid_store_delivery")
        try:
            validate_delivery_snapshot(stored.snapshot)
        except Exception:
            raise HostAutoOfferIntegrationError("invalid_store_delivery") from None
        snapshot = stored.snapshot
        if (
            snapshot.purchase_id != f"buff:{order_id}"
            or snapshot.buff_order_id != order_id
            or snapshot.account_id != self.account_id
            or snapshot.recipient_steam_id != self.recipient_steam_id
            or type(stored.revision) is not int
            or stored.revision < 1
        ):
            raise HostAutoOfferIntegrationError("host_store_identity_mismatch")
        return stored

    def _store_target(
        self,
        order_id: str,
    ) -> StoredDelivery:
        recoverable = self._bridge.list_recoverable()
        seen: set[str] = set()
        for item in recoverable:
            if type(item) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("invalid_store_delivery")
            item_order = _exact_order_id(item.snapshot.buff_order_id)
            if item_order is None or item_order in seen:
                raise HostAutoOfferIntegrationError("invalid_store_order_identity")
            seen.add(item_order)
            self._validate_store_identity(item, item_order)
        if seen - {order_id}:
            raise HostAutoOfferIntegrationError("maintenance_unrelated_store_row")
        stored = self._bridge.get_by_purchase_id(f"buff:{order_id}")
        if stored is None:
            raise HostAutoOfferIntegrationError("maintenance_store_row_required")
        return self._validate_store_identity(stored, order_id)

    @staticmethod
    def _validate_initial_target(stored: StoredDelivery) -> None:
        snapshot = stored.snapshot
        if (
            snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
            or snapshot.delivery_error != "write_result_unknown"
            or snapshot.offer_attempted_at is None
            or snapshot.offer_sent_at is not None
            or snapshot.received_at is not None
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
            or snapshot.pending_receipt is not True
            or snapshot.assetid is not None
        ):
            raise HostAutoOfferIntegrationError("maintenance_target_not_recoverable")

    @staticmethod
    def _validate_continuation_target(stored: StoredDelivery) -> None:
        snapshot = stored.snapshot
        if snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER:
            raise HostAutoOfferIntegrationError("maintenance_target_not_recoverable")
        if snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            HostRecoveryOnlyMaintenance._validate_initial_target(stored)
            return
        if snapshot.delivery_status not in _RECOVERY_ONLY_CONTINUATION_STATUSES:
            raise HostAutoOfferIntegrationError("maintenance_target_not_recoverable")
        if snapshot.delivery_status is DeliveryStatus.RECEIVED:
            if snapshot.pending_receipt is not False or _exact_assetid(snapshot.assetid) is None:
                raise HostAutoOfferIntegrationError("maintenance_receipt_not_proven")
            return
        if (
            snapshot.pending_receipt is not True
            or snapshot.assetid is not None
            or snapshot.offer_attempted_at is None
            or snapshot.offer_sent_at is None
            or snapshot.steam_tradeoffer_id is None
            or (
                snapshot.delivery_status is not DeliveryStatus.OFFER_SENT
                and snapshot.counterparty_steam_id is None
            )
        ):
            raise HostAutoOfferIntegrationError("maintenance_target_not_recoverable")

    def _admit_target(
        self,
        host_purchases: object,
    ) -> tuple[str, Mapping[str, object], StoredDelivery]:
        order_id, purchase = self._pending_host_target(host_purchases)
        if self._target_order_id is not None and self._target_order_id != order_id:
            raise HostAutoOfferIntegrationError("maintenance_target_changed")
        host_db_id = _exact_db_id(purchase.get("_db_id"))
        if host_db_id is None:
            raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
        if (
            self._target_host_db_id is not None
            and self._target_host_db_id != host_db_id
        ):
            raise HostAutoOfferIntegrationError("maintenance_target_changed")
        stored = self._store_target(order_id)
        if self._target_order_id is None:
            self._validate_initial_target(stored)
            self._target_order_id = order_id
            self._target_host_db_id = host_db_id
        else:
            self._validate_continuation_target(stored)
        return order_id, purchase, stored

    def _validate_read_result(
        self,
        current: StoredDelivery,
        result: object,
    ) -> tuple[AutoOfferResult, StoredDelivery]:
        if type(result) is not ReadOnlyStepResult or result.before != current:
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        after = result.after
        persisted = result.persisted
        decision_result = result.decision.result
        platform_result = result.platform_result
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
            or type(platform_result) is not PlatformResult
        ):
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        request = platform_result.request
        expected_capability = {
            DeliveryStatus.RESULT_UNKNOWN: PlatformCapability.READ_OFFER_STATE,
            DeliveryStatus.OFFER_SENT: PlatformCapability.READ_STEAM_TRADE_OFFER,
            DeliveryStatus.OFFER_CONFIRMATION_REQUIRED: PlatformCapability.READ_STEAM_TRADE_OFFER,
            DeliveryStatus.OFFER_CONFIRMED: PlatformCapability.READ_STEAM_TRADE_OFFER,
            DeliveryStatus.AWAITING_INVENTORY: PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        }.get(current.snapshot.delivery_status)
        if (
            expected_capability is None
            or request.capability is not expected_capability
            or request.capability not in _RECOVERY_ONLY_CAPABILITIES
            or request.purchase_id != current.snapshot.purchase_id
            or request.buff_order_id != current.snapshot.buff_order_id
            or request.account_id != current.snapshot.account_id
            or request.recipient_steam_id != current.snapshot.recipient_steam_id
            or request.revision != current.revision
        ):
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        if request.capability is PlatformCapability.READ_OFFER_STATE:
            request_bound_fields_valid = (
                request.steam_tradeoffer_id is None
                and request.counterparty_steam_id is None
                and request.host_goods_id is None
            )
        else:
            request_bound_fields_valid = (
                request.steam_tradeoffer_id
                == current.snapshot.steam_tradeoffer_id
                and request.counterparty_steam_id is None
                and request.host_goods_id is None
            )
        if not request_bound_fields_valid:
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        if request.capability in {
            PlatformCapability.SEND_OFFER,
            PlatformCapability.CONFIRM_OFFER,
            PlatformCapability.ACCEPT_OFFER,
        }:
            raise HostAutoOfferIntegrationError("maintenance_write_capability_seen")
        if decision_result is AutoOfferResult.BLOCKED:
            if persisted or after != current:
                raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
            return AutoOfferResult.BLOCKED, current
        if not persisted:
            if after != current or decision_result is not AutoOfferResult.WAITING:
                raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
            result = (
                AutoOfferResult.RESULT_UNKNOWN
                if current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
                else AutoOfferResult.WAITING
            )
            return result, current
        if after.revision != current.revision + 1:
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        if after.snapshot == current.snapshot:
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        if (
            after.snapshot.purchase_id != current.snapshot.purchase_id
            or after.snapshot.buff_order_id != current.snapshot.buff_order_id
            or after.snapshot.account_id != current.snapshot.account_id
            or after.snapshot.recipient_steam_id != current.snapshot.recipient_steam_id
        ):
            raise HostAutoOfferIntegrationError("maintenance_identity_changed")
        current_status = current.snapshot.delivery_status
        after_status = after.snapshot.delivery_status
        allowed = {
            DeliveryStatus.RESULT_UNKNOWN: {DeliveryStatus.OFFER_SENT},
            DeliveryStatus.OFFER_SENT: {
                DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
                DeliveryStatus.OFFER_CONFIRMED,
            },
            DeliveryStatus.OFFER_CONFIRMATION_REQUIRED: {
                DeliveryStatus.OFFER_CONFIRMED,
            },
            DeliveryStatus.OFFER_CONFIRMED: {DeliveryStatus.AWAITING_INVENTORY},
            DeliveryStatus.AWAITING_INVENTORY: {DeliveryStatus.RECEIVED},
        }
        if after_status not in allowed.get(current_status, set()):
            raise HostAutoOfferIntegrationError("maintenance_transition_invalid")
        self._validate_store_identity(after, current.snapshot.buff_order_id)
        self._validate_continuation_target(after)
        if after_status is DeliveryStatus.RECEIVED:
            if decision_result is not AutoOfferResult.COMPLETE:
                raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
            return AutoOfferResult.COMPLETE, after
        if decision_result is not AutoOfferResult.WAITING:
            raise HostAutoOfferIntegrationError("maintenance_read_result_invalid")
        return AutoOfferResult.WAITING, after

    def run_recovery_tick(
        self,
        host_purchases: object,
        *,
        cursor: str | None = None,
    ) -> DeliveryTickOutcome:
        """Run at most one exact read-derived maintenance transition."""

        self._require_open()
        if cursor is not None and _exact_order_id(cursor) is None:
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())
        order_id: str | None = None
        try:
            order_id, _purchase, current = self._admit_target(host_purchases)
            if current.snapshot.delivery_status is DeliveryStatus.RECEIVED:
                return DeliveryTickOutcome(
                    AutoOfferResult.COMPLETE,
                    order_id,
                    (order_id,),
                )
            if (
                current.snapshot.delivery_status
                is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
            ):
                return DeliveryTickOutcome(
                    AutoOfferResult.WAITING,
                    order_id,
                    (order_id,),
                )
            if current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
                result = self._bridge.recover_result_unknown_readonly(current)
            else:
                result = self._bridge.step(current)
            outcome, _after = self._validate_read_result(current, result)
            return DeliveryTickOutcome(outcome, order_id, (order_id,))
        except Exception:
            return DeliveryTickOutcome(
                AutoOfferResult.BLOCKED,
                order_id if order_id is not None else cursor,
                (order_id,) if order_id is not None else (),
            )

    def recover_existing_buyer_send(
        self,
        host_purchases: object,
        *,
        cursor: str | None = None,
    ) -> DeliveryTickOutcome:
        """Explicit alias for the one-target recovery tick."""

        return self.run_recovery_tick(host_purchases, cursor=cursor)

    def complete_host_receipt(self, host_purchases: object) -> bool:
        """Complete one exact Host receipt only after Store reaches RECEIVED."""

        self._require_open()
        if self._receipt_completed:
            return True
        try:
            order_id, purchase = self._receipt_host_target(host_purchases)
            stored = self._store_target(order_id)
            snapshot = stored.snapshot
            if (
                snapshot.delivery_status is not DeliveryStatus.RECEIVED
                or snapshot.pending_receipt is not False
                or _exact_assetid(snapshot.assetid) is None
            ):
                raise HostAutoOfferIntegrationError("maintenance_receipt_not_proven")
            db_id = _exact_db_id(purchase.get("_db_id"))
            if db_id is None:
                raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
            if type(purchase.get("pending_receipt")) is not bool:
                raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
            if (
                self._target_host_db_id is not None
                and db_id != self._target_host_db_id
            ):
                raise HostAutoOfferIntegrationError("maintenance_target_changed")
            host_pending = purchase.get("pending_receipt") is True
            host_asset = purchase.get("assetid")
            if not host_pending:
                if _exact_assetid(host_asset) != snapshot.assetid:
                    raise HostAutoOfferIntegrationError("host_receipt_identity_mismatch")
                self._receipt_completed = True
                return True
            if host_asset not in (None, ""):
                raise HostAutoOfferIntegrationError("host_receipt_identity_mismatch")
            writer = self._complete_purchase_receipt_by_id
            if not callable(writer):
                raise HostAutoOfferIntegrationError("receipt_writer_required")
            completed = writer(db_id, order_id, snapshot.assetid)
            if completed is not True:
                raise HostAutoOfferIntegrationError("host_receipt_write_failed")
            self._receipt_completed = True
            return True
        except HostAutoOfferIntegrationError:
            raise
        except Exception:
            raise HostAutoOfferIntegrationError("host_receipt_write_failed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bridge.close()


def build_host_recovery_only_maintenance(
    *,
    buff_client,
    complete_purchase_receipt_by_id=None,
    store_path: str | Path = _STORE_PATH,
) -> HostRecoveryOnlyMaintenance:
    """Build an explicit Host-owned recovery-only surface.

    This builder is not connected to config, workers, pipeline, or normal
    runtime startup.  Read construction needs only the exact Steam cookie;
    mobile identity_secret is deliberately not inspected.
    """

    account_id, account_steam_id = _exact_current_account()
    cookie_string = _steam_cookie_for_expected(account_steam_id)
    try:
        bridge = _build_recovery_only_host_auto_offer_bridge(
            buff_client=buff_client,
            account_id=account_id,
            account_steam_id=account_steam_id,
            steam_cookie_string=cookie_string,
            store_path=store_path,
        )
        return HostRecoveryOnlyMaintenance(
            bridge,
            complete_purchase_receipt_by_id=complete_purchase_receipt_by_id,
        )
    except HostAutoOfferIntegrationError:
        raise
    except Exception:
        raise HostAutoOfferIntegrationError(
            "recovery_only_maintenance_build_failed"
        ) from None


class HostAutoOfferIntegration:
    """Host-owned admission and bounded delivery-tick façade."""

    def __init__(
        self,
        bridge,
        *,
        complete_purchase_receipt_by_id=None,
        delete_refund_cleanup_purchase=None,
        canary_permit: CanaryPermit | None = None,
        canary_authority: CanaryAuthority | None = None,
        canary_owner_session: object | None = None,
        registration_enabled: bool = True,
    ) -> None:
        if canary_authority is not None:
            raise HostAutoOfferIntegrationError("canary_authority_injection_forbidden")
        authority = get_canary_authority()
        if type(authority) is not CanaryAuthority:
            raise HostAutoOfferIntegrationError("canary_authority_invalid")
        self._bridge = bridge
        self._account_id = bridge.account_id
        self._recipient_steam_id = bridge.recipient_steam_id
        self._complete_purchase_receipt_by_id = complete_purchase_receipt_by_id
        self._delete_refund_cleanup_purchase = delete_refund_cleanup_purchase
        self._fresh_deliveries: list[StoredDelivery] = []
        self._closed = False
        self._canary_permit = canary_permit
        self._canary_authority = authority
        self._canary_owner_session = canary_owner_session
        self._canary_preflight_consumed = False
        self._canary_completed = False
        if type(registration_enabled) is not bool:
            raise HostAutoOfferIntegrationError("registration_enabled_invalid")
        self._registration_enabled = registration_enabled
        if canary_permit is not None:
            if type(canary_permit) is not CanaryPermit:
                raise HostAutoOfferIntegrationError("canary_permit_invalid")
            if (
                canary_permit.account_id != self._account_id
                or canary_permit.recipient_steam_id != self._recipient_steam_id
            ):
                raise HostAutoOfferIntegrationError("canary_runtime_identity_mismatch")
            if not self._canary_authority.validates_owner_session(
                canary_owner_session,
                canary_permit,
            ):
                raise HostAutoOfferIntegrationError("canary_authority_not_owned")
        elif canary_owner_session is not None:
            raise HostAutoOfferIntegrationError("unexpected_canary_owner_session")

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    @property
    def is_canary(self) -> bool:
        return self._canary_permit is not None

    @property
    def registration_enabled(self) -> bool:
        return self._registration_enabled

    @property
    def canary_completed(self) -> bool:
        return self._canary_completed

    def register_committed_purchase(self, purchase: Mapping[str, object]) -> None:
        if not self._registration_enabled:
            raise HostAutoOfferIntegrationError(
                "auto_offer_registration_disabled_during_draining"
            )
        if self.is_canary:
            raise HostAutoOfferIntegrationError("canary_new_purchase_forbidden")
        try:
            with self._canary_authority.runtime_guard():
                fresh = self._bridge.register_committed_purchase(dict(purchase))
        except Exception as exc:
            raise HostAutoOfferIntegrationError(
                "auto_offer_registration_failed"
            ) from exc
        if fresh is not None:
            if type(fresh) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("auto_offer_registration_invalid")
            snapshot = fresh.snapshot
            if (
                fresh.revision != 1
                or snapshot.delivery_status is not DeliveryStatus.PENDING_DIRECTION
                or snapshot.delivery_mode is not None
                or snapshot.account_id != self._account_id
                or snapshot.recipient_steam_id != self._recipient_steam_id
            ):
                raise HostAutoOfferIntegrationError("auto_offer_registration_invalid")

    @staticmethod
    def _checkout_is_resolved() -> bool:
        try:
            return get_unresolved_checkout() is None
        except Exception:
            return False

    def _require_runtime_identity(self) -> None:
        current_id, current_steam_id = _exact_current_account()
        current_steam_id = _canonical_steam_id(current_steam_id)
        if current_id != self._account_id or current_steam_id != self._recipient_steam_id:
            raise HostAutoOfferIntegrationError("runtime_identity_mismatch")
        _steam_cookie_for_expected(current_steam_id)

    def _canary_target_order(self) -> str:
        permit = self._canary_permit
        if permit is None:
            raise HostAutoOfferIntegrationError("canary_permit_required")
        return permit.buff_order_id

    def _validate_canary_fresh_set(self) -> None:
        if not self.is_canary:
            return
        target = self._canary_target_order()
        if any(item.snapshot.buff_order_id != target for item in self._fresh_deliveries):
            raise HostAutoOfferIntegrationError("canary_non_target_fresh_delivery")
        if len(self._fresh_deliveries) > 1:
            raise HostAutoOfferIntegrationError("canary_duplicate_fresh_delivery")

    def _dispatch_fresh_deliveries(self) -> set[str]:
        """Execute the separately fenced canary fresh-delivery path only."""

        if not self._fresh_deliveries:
            return set()
        if not self.is_canary:
            raise HostAutoOfferIntegrationError("normal_fresh_delivery_forbidden")
        self._validate_canary_fresh_set()
        deferred_order_ids = {
            item.snapshot.buff_order_id for item in self._fresh_deliveries
        }
        if not self._checkout_is_resolved():
            return deferred_order_ids
        self._require_runtime_identity()

        fresh = tuple(self._fresh_deliveries)
        self._fresh_deliveries.clear()

        for delivery in fresh:
            direction_step = self._bridge.step(delivery)
            current = getattr(direction_step, "after", None)
            if type(current) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("direction_step_invalid")

            snapshot = current.snapshot
            if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
                return deferred_order_ids
            if (
                snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
                and snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
            ):
                continue
            if not (
                snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
                and snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
            ):
                raise HostAutoOfferIntegrationError("direction_step_invalid")

            send_step = self._bridge.step(current)
            current = getattr(send_step, "after", None)
            if type(current) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("send_step_invalid")

            if current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
                return deferred_order_ids
            elif current.snapshot.delivery_status is not DeliveryStatus.OFFER_SENT:
                raise HostAutoOfferIntegrationError("send_step_invalid")
        return deferred_order_ids

    def _host_pending_by_order(self, host_purchases: object) -> dict[str, Mapping[str, object]]:
        return _preflight_host_pending_by_order(host_purchases)

    def _validate_store_delivery(self, stored: object, order_id: str) -> StoredDelivery:
        if type(stored) is not StoredDelivery:
            raise HostAutoOfferIntegrationError("invalid_store_delivery")
        snapshot = stored.snapshot
        if (
            snapshot.purchase_id != f"buff:{order_id}"
            or snapshot.buff_order_id != order_id
            or snapshot.account_id != self._account_id
            or snapshot.recipient_steam_id != self._recipient_steam_id
        ):
            raise HostAutoOfferIntegrationError("store_identity_mismatch")
        return stored

    def _recoverable_by_order(self) -> dict[str, StoredDelivery]:
        recoverable: dict[str, StoredDelivery] = {}
        for stored in self._bridge.list_recoverable():
            if type(stored) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("invalid_store_delivery")
            order_id = _exact_order_id(stored.snapshot.buff_order_id)
            if order_id is None or order_id in recoverable:
                raise HostAutoOfferIntegrationError("invalid_store_order_identity")
            recoverable[order_id] = self._validate_store_delivery(stored, order_id)
        return recoverable

    def _validate_normal_store_state(
        self,
        stored: object,
        order_id: str,
    ) -> StoredDelivery:
        stored = self._validate_store_delivery(stored, order_id)
        if type(stored.revision) is not int or stored.revision < 1:
            raise HostAutoOfferIntegrationError("invalid_store_revision")
        try:
            validate_delivery_snapshot(stored.snapshot)
        except Exception:
            raise HostAutoOfferIntegrationError("invalid_store_delivery") from None
        snapshot = stored.snapshot
        if snapshot.delivery_status is DeliveryStatus.RECEIVED:
            if snapshot.pending_receipt is not False or _exact_assetid(snapshot.assetid) is None:
                raise HostAutoOfferIntegrationError("store_receipt_not_proven")
            return stored
        if snapshot.pending_receipt is not True or snapshot.assetid is not None:
            raise HostAutoOfferIntegrationError("invalid_store_delivery")
        if snapshot.delivery_status in {
            DeliveryStatus.BLOCKED,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.REFUNDED,
        }:
            raise HostAutoOfferIntegrationError("unsafe_store_terminal_state")
        return stored

    def _normal_store_by_order(
        self,
        host_pending: Mapping[str, Mapping[str, object]],
        recoverable: Mapping[str, StoredDelivery],
    ) -> dict[str, StoredDelivery]:
        stored_by_order = {
            order_id: self._validate_normal_store_state(stored, order_id)
            for order_id, stored in recoverable.items()
        }
        for order_id in host_pending:
            if order_id in stored_by_order:
                continue
            stored = self._bridge.get_by_purchase_id(f"buff:{order_id}")
            if stored is None:
                raise HostAutoOfferIntegrationError("host_store_set_mismatch")
            stored = self._validate_normal_store_state(stored, order_id)
            if stored.snapshot.delivery_status is not DeliveryStatus.RECEIVED:
                raise HostAutoOfferIntegrationError("host_store_set_mismatch")
            stored_by_order[order_id] = stored
        return stored_by_order

    @staticmethod
    def _normal_sets_match(
        host_pending: Mapping[str, Mapping[str, object]],
        stored_by_order: Mapping[str, StoredDelivery],
    ) -> bool:
        if any(order_id not in stored_by_order for order_id in host_pending):
            return False
        return all(
            order_id in host_pending
            or stored.snapshot.delivery_status
            is DeliveryStatus.REFUND_CLEANUP_PENDING
            for order_id, stored in stored_by_order.items()
        )

    @staticmethod
    def _normal_delivery_policy(stored: StoredDelivery) -> str:
        snapshot = stored.snapshot
        if snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            return "result_unknown"
        if snapshot.delivery_status is DeliveryStatus.RECEIVED:
            return "receipt"
        if snapshot.delivery_status is DeliveryStatus.OFFER_TERMINATED:
            return "read"
        if snapshot.delivery_status is DeliveryStatus.REFUND_CLEANUP_PENDING:
            return "cleanup"
        if snapshot.delivery_status is DeliveryStatus.OFFER_ACCEPT_ATTEMPTED:
            return (
                "read"
                if snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
                else "block"
            )
        if (
            snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
            and snapshot.delivery_status
            is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
        ):
            return "confirm"
        if (
            snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
            and snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
        ):
            return "accept"
        if (
            snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
            and snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
        ):
            return "send"
        policy = _persisted_recovery_policy(stored)
        return "block" if policy == "confirm" else policy

    @staticmethod
    def _visit_order_ids(
        order_ids: Sequence[str],
        cursor: str | None,
    ) -> tuple[str, ...]:
        ordered = tuple(sorted(order_ids))
        if not ordered:
            return ()
        start = 0 if cursor is None else bisect_right(ordered, cursor)
        if start == len(ordered):
            start = 0
        count = min(MAX_DELIVERY_ORDERS_PER_TICK, len(ordered))
        return tuple(ordered[(start + offset) % len(ordered)] for offset in range(count))

    def _validate_canary_host_target(
        self,
        host_pending: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, object]:
        permit = self._canary_permit
        if permit is None:
            raise HostAutoOfferIntegrationError("canary_permit_required")
        if set(host_pending) != {permit.buff_order_id}:
            raise HostAutoOfferIntegrationError("canary_host_target_not_exclusive")
        purchase = host_pending[permit.buff_order_id]
        if purchase.get("_db_id") != permit.host_db_id:
            raise HostAutoOfferIntegrationError("canary_host_db_id_mismatch")
        return purchase

    def _terminal_canary_target(self) -> StoredDelivery | None:
        permit = self._canary_permit
        if permit is None:
            return None
        stored = self._bridge.get_by_purchase_id(permit.purchase_id)
        if stored is None:
            return None
        stored = self._validate_store_delivery(stored, permit.buff_order_id)
        if (
            stored.snapshot.delivery_status is DeliveryStatus.RECEIVED
            and stored.snapshot.pending_receipt is False
            and _exact_assetid(stored.snapshot.assetid) is not None
        ):
            return stored
        return None

    def _prepare_canary_before_dispatch(
        self,
        host_pending: dict[str, Mapping[str, object]],
        recoverable: dict[str, StoredDelivery],
    ) -> dict[str, StoredDelivery]:
        permit = self._canary_permit
        if permit is None:
            return recoverable
        target_purchase = self._validate_canary_host_target(host_pending)
        target = permit.buff_order_id
        if set(recoverable) - {target}:
            raise HostAutoOfferIntegrationError("canary_unrelated_store_row")
        self._validate_canary_fresh_set()

        if not self._canary_preflight_consumed:
            stored = self._bridge.get_by_purchase_id(permit.purchase_id)
            if permit.expected_store_present:
                if stored is None:
                    raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
                stored = self._validate_store_delivery(stored, target)
                if (
                    stored.revision != permit.expected_store_revision
                    or stored.snapshot.delivery_status.value != permit.expected_store_status
                    or stored.snapshot.steam_tradeoffer_id
                    != permit.expected_store_tradeoffer_id
                ):
                    raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
                if stored.snapshot.delivery_status is DeliveryStatus.RECEIVED:
                    if recoverable:
                        raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
                elif recoverable != {target: stored}:
                    raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
            else:
                if stored is not None or recoverable:
                    raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
                fresh = self._bridge.register_committed_purchase(dict(target_purchase))
                if type(fresh) is not StoredDelivery:
                    raise HostAutoOfferIntegrationError("canary_initial_registration_failed")
                if (
                    fresh.revision != 1
                    or fresh.snapshot.purchase_id != permit.purchase_id
                    or fresh.snapshot.buff_order_id != target
                    or fresh.snapshot.account_id != permit.account_id
                    or fresh.snapshot.recipient_steam_id != permit.recipient_steam_id
                    or fresh.snapshot.delivery_status is not DeliveryStatus.PENDING_DIRECTION
                ):
                    raise HostAutoOfferIntegrationError("canary_initial_registration_invalid")
                self._fresh_deliveries.append(fresh)
                self._validate_canary_fresh_set()
                recoverable = self._recoverable_by_order()
                if recoverable != {target: fresh}:
                    raise HostAutoOfferIntegrationError("canary_store_snapshot_changed")
            self._canary_preflight_consumed = True

        if not recoverable:
            if self._terminal_canary_target() is None:
                raise HostAutoOfferIntegrationError("canary_store_target_not_exclusive")
            return recoverable
        if set(recoverable) != {target}:
            raise HostAutoOfferIntegrationError("canary_store_target_not_exclusive")
        self._validate_canary_fresh_set()
        return recoverable

    def _validate_canary_current_sets(
        self,
        host_pending: Mapping[str, Mapping[str, object]],
        recoverable: Mapping[str, StoredDelivery],
    ) -> None:
        if not self.is_canary:
            return
        target = self._canary_target_order()
        self._validate_canary_host_target(host_pending)
        if set(recoverable) != {target}:
            raise HostAutoOfferIntegrationError("canary_store_target_not_exclusive")
        self._validate_canary_fresh_set()

    def _write_back_received(
        self,
        purchase: Mapping[str, object],
        stored: StoredDelivery,
    ) -> None:
        order_id = _exact_order_id(purchase.get("buff_order_id"))
        db_id = _exact_db_id(purchase.get("_db_id"))
        if order_id is None or db_id is None:
            raise HostAutoOfferIntegrationError("invalid_host_pending_identity")
        stored = self._validate_store_delivery(stored, order_id)
        snapshot = stored.snapshot
        assetid = _exact_assetid(snapshot.assetid)
        if (
            snapshot.delivery_status is not DeliveryStatus.RECEIVED
            or snapshot.pending_receipt is not False
            or assetid is None
        ):
            raise HostAutoOfferIntegrationError("store_receipt_not_proven")
        writer = self._complete_purchase_receipt_by_id
        if not callable(writer):
            raise HostAutoOfferIntegrationError("receipt_writer_required")
        target = CanaryWriteTarget(
            action="host_receipt",
            purchase_id=snapshot.purchase_id,
            buff_order_id=order_id,
            account_id=snapshot.account_id,
            recipient_steam_id=snapshot.recipient_steam_id,
            host_db_id=db_id,
            assetid=assetid,
        )
        try:
            if self.is_canary:
                if self._canary_owner_session is None:
                    raise HostAutoOfferIntegrationError("canary_owner_session_required")
                guard = self._canary_owner_session.external_write_guard(target)
            else:
                guard = self._canary_authority.external_write_guard(target)
            with guard:
                completed = writer(db_id, order_id, assetid)
        except Exception:
            raise HostAutoOfferIntegrationError("host_receipt_write_failed") from None
        if completed is not True:
            raise HostAutoOfferIntegrationError("host_receipt_write_failed")

    def _sync_canary_terminal_received(
        self,
        host_pending: dict[str, Mapping[str, object]],
        recoverable: Mapping[str, StoredDelivery],
    ) -> None:
        for order_id in tuple(host_pending):
            if order_id in recoverable:
                continue
            stored = self._bridge.get_by_purchase_id(f"buff:{order_id}")
            if stored is None:
                raise HostAutoOfferIntegrationError("host_store_set_mismatch")
            stored = self._validate_store_delivery(stored, order_id)
            if stored.snapshot.delivery_status is not DeliveryStatus.RECEIVED:
                raise HostAutoOfferIntegrationError("host_store_set_mismatch")
            self._write_back_received(host_pending[order_id], stored)
            del host_pending[order_id]

    def _verify_canary_confirmation_identity(
        self,
        current: StoredDelivery,
    ) -> tuple[AutoOfferResult, StoredDelivery, bool]:
        permit = self._canary_permit
        if permit is None:
            raise HostAutoOfferIntegrationError("canary_permit_required")
        step_result = self._bridge.read_confirmation_state(current)
        after = getattr(step_result, "after", None)
        persisted = getattr(step_result, "persisted", None)
        decision = getattr(step_result, "decision", None)
        decision_result = getattr(decision, "result", None)
        platform_result = getattr(step_result, "platform_result", None)
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
            or type(platform_result) is not PlatformResult
        ):
            raise HostAutoOfferIntegrationError("confirmation_read_invalid")
        if decision_result is AutoOfferResult.BLOCKED:
            return AutoOfferResult.BLOCKED, current, False
        if persisted:
            if (
                after.revision != current.revision + 1
                or after.snapshot == current.snapshot
                or after.snapshot.delivery_status is not DeliveryStatus.OFFER_CONFIRMED
                or after.snapshot.steam_tradeoffer_id
                != current.snapshot.steam_tradeoffer_id
            ):
                raise HostAutoOfferIntegrationError("confirmation_read_invalid")
            return AutoOfferResult.WAITING, after, False
        if after != current:
            raise HostAutoOfferIntegrationError("confirmation_read_invalid")
        if decision_result is not AutoOfferResult.WAITING:
            return AutoOfferResult.BLOCKED, current, False
        evidence = platform_result.evidence
        if platform_result.status is not PlatformResultStatus.SUCCESS:
            return AutoOfferResult.WAITING, current, False
        if (
            type(evidence) is not SteamTradeOfferEvidence
            or evidence.steam_tradeoffer_id != current.snapshot.steam_tradeoffer_id
            or evidence.account_steam_id != permit.recipient_steam_id
            or evidence.counterparty_steam_id
            != permit.expected_counterparty_steam_id
            or evidence.is_our_offer is not permit.expected_is_our_offer
            or permit.expected_is_our_offer is not True
            or evidence.items_to_give != ()
            or evidence.lifecycle
            is not SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
        ):
            return AutoOfferResult.BLOCKED, current, False
        return AutoOfferResult.WAITING, current, True

    def _recover_canary_persisted_delivery(
        self,
        delivery: StoredDelivery,
    ) -> tuple[AutoOfferResult, StoredDelivery]:
        current = delivery
        if (
            self.is_canary
            and current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
        ):
            return AutoOfferResult.RESULT_UNKNOWN, current
        for _step_index in range(_MAX_CANARY_RECOVERY_STEPS_PER_DELIVERY):
            policy = _persisted_recovery_policy(current)
            if policy == "wait":
                return AutoOfferResult.WAITING, current
            if policy == "confirm":
                if self.is_canary:
                    read_result, read_after, may_confirm = (
                        self._verify_canary_confirmation_identity(current)
                    )
                    if not may_confirm:
                        return read_result, read_after
                before = current
                step_result = self._bridge.step(current)
                attempted = getattr(step_result, "attempted", None)
                after = getattr(step_result, "after", None)
                platform_result = getattr(step_result, "platform_result", None)
                platform_status = getattr(platform_result, "status", None)
                if (
                    type(attempted) is not StoredDelivery
                    or type(after) is not StoredDelivery
                    or attempted.revision != before.revision + 1
                    or attempted.snapshot.delivery_mode
                    is not DeliveryMode.BUYER_SENDS_OFFER
                    or attempted.snapshot.delivery_status
                    is not DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
                    or attempted.snapshot.purchase_id != before.snapshot.purchase_id
                    or attempted.snapshot.buff_order_id != before.snapshot.buff_order_id
                    or attempted.snapshot.account_id != before.snapshot.account_id
                    or attempted.snapshot.recipient_steam_id
                    != before.snapshot.recipient_steam_id
                    or attempted.snapshot.steam_tradeoffer_id
                    != before.snapshot.steam_tradeoffer_id
                    or after.snapshot.delivery_mode != attempted.snapshot.delivery_mode
                    or after.snapshot.purchase_id != attempted.snapshot.purchase_id
                    or after.snapshot.buff_order_id != attempted.snapshot.buff_order_id
                    or after.snapshot.account_id != attempted.snapshot.account_id
                    or after.snapshot.recipient_steam_id
                    != attempted.snapshot.recipient_steam_id
                    or after.snapshot.steam_tradeoffer_id
                    != attempted.snapshot.steam_tradeoffer_id
                ):
                    raise HostAutoOfferIntegrationError("confirmation_step_invalid")
                if after == attempted:
                    if platform_status not in {
                        PlatformResultStatus.UNSUPPORTED,
                        PlatformResultStatus.TIMEOUT,
                        PlatformResultStatus.FAILURE,
                        PlatformResultStatus.MALFORMED,
                    }:
                        raise HostAutoOfferIntegrationError("confirmation_step_invalid")
                    return AutoOfferResult.BLOCKED, attempted
                if after.revision != attempted.revision + 1:
                    raise HostAutoOfferIntegrationError("confirmation_step_invalid")
                if after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
                    if (
                        platform_status is not PlatformResultStatus.RESULT_UNKNOWN
                        or after.snapshot.delivery_error != "write_result_unknown"
                    ):
                        raise HostAutoOfferIntegrationError("confirmation_step_invalid")
                    return AutoOfferResult.RESULT_UNKNOWN, after
                if after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED:
                    if platform_status is not PlatformResultStatus.SUCCESS:
                        raise HostAutoOfferIntegrationError("confirmation_step_invalid")
                    return AutoOfferResult.WAITING, after
                raise HostAutoOfferIntegrationError("confirmation_step_invalid")
            if policy != "read":
                return AutoOfferResult.BLOCKED, current

            step_result = self._bridge.step(current)
            after = getattr(step_result, "after", None)
            persisted = getattr(step_result, "persisted", None)
            decision = getattr(step_result, "decision", None)
            decision_result = getattr(decision, "result", None)
            if (
                type(after) is not StoredDelivery
                or type(persisted) is not bool
                or type(decision_result) is not AutoOfferResult
            ):
                raise HostAutoOfferIntegrationError("recovery_step_invalid")
            if decision_result is AutoOfferResult.BLOCKED:
                return AutoOfferResult.BLOCKED, current
            if not persisted:
                if after != current:
                    raise HostAutoOfferIntegrationError("recovery_step_invalid")
                return AutoOfferResult.WAITING, current
            if (
                after.revision != current.revision + 1
                or after.snapshot == current.snapshot
            ):
                raise HostAutoOfferIntegrationError("recovery_step_invalid")

            current = after
            if current.snapshot.delivery_status is DeliveryStatus.RECEIVED:
                return AutoOfferResult.COMPLETE, current

        return AutoOfferResult.BLOCKED, current

    def _step_normal_read_once(self, current: StoredDelivery) -> AutoOfferResult:
        step_result = self._bridge.step(current)
        after = getattr(step_result, "after", None)
        persisted = getattr(step_result, "persisted", None)
        decision = getattr(step_result, "decision", None)
        decision_result = getattr(decision, "result", None)
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
        ):
            raise HostAutoOfferIntegrationError("delivery_tick_step_invalid")
        if decision_result is AutoOfferResult.BLOCKED:
            return AutoOfferResult.BLOCKED
        if not persisted:
            if after != current or decision_result is not AutoOfferResult.WAITING:
                raise HostAutoOfferIntegrationError("delivery_tick_step_invalid")
            return AutoOfferResult.WAITING
        if after.revision != current.revision + 1 or after.snapshot == current.snapshot:
            raise HostAutoOfferIntegrationError("delivery_tick_step_invalid")
        after = self._validate_normal_store_state(
            after,
            current.snapshot.buff_order_id,
        )
        if after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            return AutoOfferResult.RESULT_UNKNOWN
        if decision_result is AutoOfferResult.RESULT_UNKNOWN:
            raise HostAutoOfferIntegrationError("delivery_tick_step_invalid")
        if decision_result not in {
            AutoOfferResult.WAITING,
            AutoOfferResult.COMPLETE,
        }:
            raise HostAutoOfferIntegrationError("delivery_tick_step_invalid")
        return AutoOfferResult.WAITING

    @staticmethod
    def _same_refunded_cleanup_target(
        current: StoredDelivery,
        candidate: object,
    ) -> bool:
        if type(candidate) is not StoredDelivery:
            return False
        if candidate.revision != current.revision + 1:
            return False
        expected = replace(
            current.snapshot,
            delivery_status=DeliveryStatus.REFUNDED,
        )
        return candidate.snapshot == expected

    def _step_refund_cleanup_once(
        self,
        host_purchases: object,
        current: StoredDelivery,
    ) -> AutoOfferResult:
        snapshot = current.snapshot
        if (
            snapshot.delivery_status
            is not DeliveryStatus.REFUND_CLEANUP_PENDING
            or current.revision < 1
            or snapshot.pending_receipt is not True
            or snapshot.assetid is not None
            or snapshot.received_at is not None
        ):
            raise HostAutoOfferIntegrationError("refund_cleanup_target_invalid")
        self._require_runtime_identity()
        if not self._checkout_is_resolved():
            return AutoOfferResult.WAITING

        refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
        if refreshed != current:
            raise HostAutoOfferIntegrationError("refund_cleanup_store_changed")
        expected_present = _cleanup_expected_present(
            host_purchases,
            snapshot.buff_order_id,
        )
        callback = self._delete_refund_cleanup_purchase
        if not callable(callback):
            raise HostAutoOfferIntegrationError("refund_cleanup_writer_required")
        try:
            deleted = callback(snapshot.buff_order_id, expected_present)
        except Exception:
            deleted = False
        if deleted is True:
            try:
                completed = self._bridge.complete_refund_cleanup(current)
            except Exception:
                completed = None
            if type(completed) is StoredDelivery:
                if not self._same_refunded_cleanup_target(current, completed):
                    raise HostAutoOfferIntegrationError(
                        "refund_cleanup_result_invalid"
                    )
                return AutoOfferResult.WAITING

        concurrent = self._bridge.get_by_purchase_id(snapshot.purchase_id)
        if self._same_refunded_cleanup_target(current, concurrent):
            return AutoOfferResult.WAITING
        return AutoOfferResult.BLOCKED

    @staticmethod
    def _is_c2a_result_unknown(stored: StoredDelivery) -> bool:
        snapshot = stored.snapshot
        return (
            snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
            and snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
            and snapshot.offer_attempted_at is not None
            and snapshot.steam_tradeoffer_id is None
            and snapshot.counterparty_steam_id is None
        )

    @staticmethod
    def _is_c2b_result_unknown(stored: StoredDelivery) -> bool:
        snapshot = stored.snapshot
        return (
            snapshot.delivery_mode is DeliveryMode.BUYER_SENDS_OFFER
            and snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
            and snapshot.offer_attempted_at is not None
            and snapshot.offer_sent_at is not None
            and snapshot.steam_tradeoffer_id is not None
            and snapshot.counterparty_steam_id is not None
        )

    @staticmethod
    def _is_c3_accept_result_unknown(stored: StoredDelivery) -> bool:
        snapshot = stored.snapshot
        return (
            snapshot.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
            and snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
            and snapshot.offer_attempted_at is None
            and snapshot.offer_sent_at is None
            and snapshot.steam_tradeoffer_id is not None
            and snapshot.counterparty_steam_id is not None
        )

    def _step_normal_send_once(
        self,
        host_purchase: Mapping[str, object],
        current: StoredDelivery,
    ) -> AutoOfferResult:
        snapshot = current.snapshot
        db_id = _exact_db_id(host_purchase.get("_db_id"))
        if (
            db_id is None
            or host_purchase.get("buff_order_id") != snapshot.buff_order_id
            or host_purchase.get("pending_receipt") is not True
            or host_purchase.get("assetid") not in (None, "")
            or snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.AWAITING_OFFER
            or snapshot.steam_tradeoffer_id is not None
            or snapshot.counterparty_steam_id is not None
        ):
            raise HostAutoOfferIntegrationError("normal_send_target_invalid")
        target = CanaryWriteTarget(
            action="auto_offer_send",
            purchase_id=snapshot.purchase_id,
            buff_order_id=snapshot.buff_order_id,
            account_id=snapshot.account_id,
            recipient_steam_id=snapshot.recipient_steam_id,
            host_db_id=db_id,
        )
        with self._canary_authority.normal_delivery_write_guard(target):
            refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
            refreshed = self._validate_normal_store_state(
                refreshed,
                snapshot.buff_order_id,
            )
            if refreshed != current:
                raise HostAutoOfferIntegrationError("normal_send_store_changed")
            proof = self._bridge.read_send_authority(refreshed)
            send_result = self._bridge.send_offer_with_authority(refreshed, proof)

            before = getattr(send_result, "before", None)
            attempted = getattr(send_result, "attempted", None)
            after = getattr(send_result, "after", None)
            platform_result = getattr(send_result, "platform_result", None)
            if (
                before != refreshed
                or type(attempted) is not StoredDelivery
                or type(after) is not StoredDelivery
                or type(platform_result) is not PlatformResult
                or attempted.revision != refreshed.revision + 1
                or after.revision != attempted.revision + 1
                or attempted.snapshot.delivery_status
                is not DeliveryStatus.OFFER_ATTEMPTED
                or after.snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
                or after.snapshot.delivery_error != "write_result_unknown"
                or attempted.snapshot.steam_tradeoffer_id is not None
                or attempted.snapshot.counterparty_steam_id is not None
                or after.snapshot.steam_tradeoffer_id is not None
                or after.snapshot.counterparty_steam_id is not None
                or platform_result.request.capability
                is not PlatformCapability.SEND_OFFER
                or platform_result.request.revision != attempted.revision
                or platform_result.request.purchase_id != snapshot.purchase_id
                or platform_result.request.buff_order_id != snapshot.buff_order_id
                or platform_result.request.account_id != snapshot.account_id
                or platform_result.request.recipient_steam_id
                != snapshot.recipient_steam_id
            ):
                raise HostAutoOfferIntegrationError("normal_send_result_invalid")
        return AutoOfferResult.RESULT_UNKNOWN

    def _step_normal_confirm_once(
        self,
        host_purchase: Mapping[str, object],
        current: StoredDelivery,
    ) -> AutoOfferResult:
        snapshot = current.snapshot
        db_id = _exact_db_id(host_purchase.get("_db_id"))
        if (
            db_id is None
            or host_purchase.get("buff_order_id") != snapshot.buff_order_id
            or host_purchase.get("pending_receipt") is not True
            or host_purchase.get("assetid") not in (None, "")
            or snapshot.delivery_mode is not DeliveryMode.BUYER_SENDS_OFFER
            or snapshot.delivery_status
            is not DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
            or snapshot.steam_tradeoffer_id is None
            or snapshot.counterparty_steam_id is None
        ):
            raise HostAutoOfferIntegrationError("normal_confirm_target_invalid")
        target = CanaryWriteTarget(
            action="auto_offer_confirm",
            purchase_id=snapshot.purchase_id,
            buff_order_id=snapshot.buff_order_id,
            account_id=snapshot.account_id,
            recipient_steam_id=snapshot.recipient_steam_id,
            host_db_id=db_id,
        )
        with self._canary_authority.normal_delivery_write_guard(target):
            refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
            refreshed = self._validate_normal_store_state(
                refreshed,
                snapshot.buff_order_id,
            )
            if refreshed != current:
                raise HostAutoOfferIntegrationError("normal_confirm_store_changed")
            authority_result = self._bridge.read_confirmation_authority(refreshed)
            if (
                type(authority_result) is not ConfirmationAuthorityReadResult
                or authority_result.before != refreshed
            ):
                raise HostAutoOfferIntegrationError(
                    "normal_confirmation_authority_invalid"
                )
            steam_result = authority_result.steam_result
            proof = authority_result.proof
            if steam_result is None:
                if proof is not None:
                    raise HostAutoOfferIntegrationError(
                        "normal_confirmation_authority_invalid"
                    )
                return AutoOfferResult.WAITING
            decision_result = steam_result.decision.result
            if decision_result is AutoOfferResult.BLOCKED:
                if steam_result.persisted or steam_result.after != refreshed or proof is not None:
                    raise HostAutoOfferIntegrationError(
                        "normal_confirmation_authority_invalid"
                    )
                return AutoOfferResult.BLOCKED
            if steam_result.persisted:
                after = steam_result.after
                if (
                    proof is not None
                    or decision_result is not AutoOfferResult.WAITING
                    or after.revision != refreshed.revision + 1
                    or after.snapshot.delivery_status not in {
                        DeliveryStatus.OFFER_CONFIRMED,
                        DeliveryStatus.OFFER_TERMINATED,
                    }
                    or after.snapshot.steam_tradeoffer_id
                    != refreshed.snapshot.steam_tradeoffer_id
                    or after.snapshot.counterparty_steam_id
                    != refreshed.snapshot.counterparty_steam_id
                ):
                    raise HostAutoOfferIntegrationError(
                        "normal_confirmation_authority_invalid"
                    )
                return AutoOfferResult.WAITING
            if (
                steam_result.after != refreshed
                or decision_result is not AutoOfferResult.WAITING
            ):
                raise HostAutoOfferIntegrationError(
                    "normal_confirmation_authority_invalid"
                )
            if proof is None:
                return AutoOfferResult.WAITING

            confirm_result = self._bridge.confirm_offer_with_authority(
                refreshed,
                proof,
            )
            before = getattr(confirm_result, "before", None)
            attempted = getattr(confirm_result, "attempted", None)
            after = getattr(confirm_result, "after", None)
            platform_result = getattr(confirm_result, "platform_result", None)
            if (
                before != refreshed
                or type(attempted) is not StoredDelivery
                or type(after) is not StoredDelivery
                or type(platform_result) is not PlatformResult
                or attempted.revision != refreshed.revision + 1
                or attempted.snapshot.delivery_status
                is not DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
                or attempted.snapshot.steam_tradeoffer_id
                != snapshot.steam_tradeoffer_id
                or attempted.snapshot.counterparty_steam_id
                != snapshot.counterparty_steam_id
                or platform_result.request.capability
                is not PlatformCapability.CONFIRM_OFFER
                or platform_result.request.revision != attempted.revision
                or platform_result.request.purchase_id != snapshot.purchase_id
                or platform_result.request.buff_order_id != snapshot.buff_order_id
                or platform_result.request.account_id != snapshot.account_id
                or platform_result.request.recipient_steam_id
                != snapshot.recipient_steam_id
                or platform_result.request.steam_tradeoffer_id
                != snapshot.steam_tradeoffer_id
            ):
                raise HostAutoOfferIntegrationError("normal_confirm_result_invalid")
            if platform_result.status is PlatformResultStatus.SUCCESS:
                if (
                    after.revision != attempted.revision + 1
                    or after.snapshot.delivery_status
                    is not DeliveryStatus.OFFER_CONFIRMED
                ):
                    raise HostAutoOfferIntegrationError("normal_confirm_result_invalid")
            elif platform_result.status is PlatformResultStatus.RESULT_UNKNOWN:
                if (
                    after.revision != attempted.revision + 1
                    or after.snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
                    or after.snapshot.delivery_error != "write_result_unknown"
                ):
                    raise HostAutoOfferIntegrationError("normal_confirm_result_invalid")
                return AutoOfferResult.RESULT_UNKNOWN
            elif after != attempted:
                raise HostAutoOfferIntegrationError("normal_confirm_result_invalid")
        return AutoOfferResult.WAITING

    def _step_normal_accept_once(
        self,
        host_purchase: Mapping[str, object],
        current: StoredDelivery,
    ) -> AutoOfferResult:
        snapshot = current.snapshot
        db_id = _exact_db_id(host_purchase.get("_db_id"))
        goods_id = _exact_goods_id(host_purchase.get("goods_id"))
        if (
            db_id is None
            or goods_id is None
            or host_purchase.get("buff_order_id") != snapshot.buff_order_id
            or host_purchase.get("pending_receipt") is not True
            or host_purchase.get("assetid") not in (None, "")
            or snapshot.delivery_mode is not DeliveryMode.SELLER_SENDS_OFFER
            or snapshot.delivery_status is not DeliveryStatus.OFFER_CONFIRMED
            or snapshot.steam_tradeoffer_id is None
            or snapshot.counterparty_steam_id is None
            or snapshot.offer_attempted_at is not None
            or snapshot.offer_sent_at is not None
        ):
            raise HostAutoOfferIntegrationError("normal_accept_target_invalid")
        target = CanaryWriteTarget(
            action="auto_offer_accept",
            purchase_id=snapshot.purchase_id,
            buff_order_id=snapshot.buff_order_id,
            account_id=snapshot.account_id,
            recipient_steam_id=snapshot.recipient_steam_id,
            host_db_id=db_id,
            host_goods_id=goods_id,
        )
        with self._canary_authority.normal_delivery_write_guard(target):
            refreshed = self._bridge.get_by_purchase_id(snapshot.purchase_id)
            refreshed = self._validate_normal_store_state(
                refreshed,
                snapshot.buff_order_id,
            )
            if refreshed != current:
                raise HostAutoOfferIntegrationError("normal_accept_store_changed")
            authority_result = self._bridge.read_seller_accept_authority(
                refreshed,
                goods_id,
            )
            if (
                type(authority_result) is not SellerAcceptAuthorityReadResult
                or authority_result.before != refreshed
                or authority_result.host_goods_id != goods_id
            ):
                raise HostAutoOfferIntegrationError(
                    "normal_accept_authority_invalid"
                )
            steam_result = authority_result.steam_result
            proof = authority_result.proof
            if steam_result is None:
                if proof is not None:
                    raise HostAutoOfferIntegrationError(
                        "normal_accept_authority_invalid"
                    )
                return AutoOfferResult.WAITING
            decision_result = steam_result.decision.result
            if decision_result is AutoOfferResult.BLOCKED:
                if steam_result.persisted or steam_result.after != refreshed or proof is not None:
                    raise HostAutoOfferIntegrationError(
                        "normal_accept_authority_invalid"
                    )
                return AutoOfferResult.BLOCKED
            if steam_result.persisted:
                after = steam_result.after
                if (
                    proof is not None
                    or decision_result is not AutoOfferResult.WAITING
                    or after.revision != refreshed.revision + 1
                    or after.snapshot.delivery_status not in {
                        DeliveryStatus.AWAITING_INVENTORY,
                        DeliveryStatus.OFFER_TERMINATED,
                    }
                    or after.snapshot.steam_tradeoffer_id
                    != refreshed.snapshot.steam_tradeoffer_id
                    or after.snapshot.counterparty_steam_id
                    != refreshed.snapshot.counterparty_steam_id
                ):
                    raise HostAutoOfferIntegrationError(
                        "normal_accept_authority_invalid"
                    )
                return AutoOfferResult.WAITING
            if (
                steam_result.after != refreshed
                or decision_result is not AutoOfferResult.WAITING
            ):
                raise HostAutoOfferIntegrationError(
                    "normal_accept_authority_invalid"
                )
            if proof is None:
                return AutoOfferResult.WAITING

            final_current = self._bridge.get_by_purchase_id(snapshot.purchase_id)
            final_current = self._validate_normal_store_state(
                final_current,
                snapshot.buff_order_id,
            )
            if final_current != refreshed:
                raise HostAutoOfferIntegrationError(
                    "normal_accept_store_changed"
                )
            accept_result = self._bridge.accept_offer_with_authority(
                final_current,
                proof,
            )
            if type(accept_result) is not AcceptOfferStepResult:
                raise HostAutoOfferIntegrationError("normal_accept_result_invalid")
            before = accept_result.before
            attempted = accept_result.attempted
            after = accept_result.after
            platform_result = accept_result.platform_result
            request = platform_result.request
            if (
                before != final_current
                or attempted.revision != final_current.revision + 1
                or attempted.snapshot.delivery_status
                is not DeliveryStatus.OFFER_ACCEPT_ATTEMPTED
                or attempted.snapshot.steam_tradeoffer_id
                != snapshot.steam_tradeoffer_id
                or attempted.snapshot.counterparty_steam_id
                != snapshot.counterparty_steam_id
                or request.capability is not PlatformCapability.ACCEPT_OFFER
                or request.revision != attempted.revision
                or request.purchase_id != snapshot.purchase_id
                or request.buff_order_id != snapshot.buff_order_id
                or request.account_id != snapshot.account_id
                or request.recipient_steam_id != snapshot.recipient_steam_id
                or request.steam_tradeoffer_id != snapshot.steam_tradeoffer_id
                or request.counterparty_steam_id
                != snapshot.counterparty_steam_id
                or request.host_goods_id is not None
            ):
                raise HostAutoOfferIntegrationError("normal_accept_result_invalid")
            if (
                platform_result.status is PlatformResultStatus.FAILURE
                and platform_result.detail == "write_preflight_failed"
            ):
                if after != attempted:
                    raise HostAutoOfferIntegrationError(
                        "normal_accept_result_invalid"
                    )
                return AutoOfferResult.WAITING
            if (
                after.revision != attempted.revision + 1
                or after.snapshot.delivery_status is not DeliveryStatus.RESULT_UNKNOWN
                or after.snapshot.delivery_error != "write_result_unknown"
            ):
                raise HostAutoOfferIntegrationError("normal_accept_result_invalid")
        return AutoOfferResult.RESULT_UNKNOWN

    def _step_result_unknown_readonly_once(
        self,
        current: StoredDelivery,
    ) -> AutoOfferResult:
        result = self._bridge.recover_result_unknown_readonly(current)
        after = getattr(result, "after", None)
        persisted = getattr(result, "persisted", None)
        decision = getattr(result, "decision", None)
        decision_result = getattr(decision, "result", None)
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
        ):
            raise HostAutoOfferIntegrationError("result_unknown_recovery_invalid")
        if decision_result is AutoOfferResult.BLOCKED:
            if persisted or after != current:
                raise HostAutoOfferIntegrationError("result_unknown_recovery_invalid")
            return AutoOfferResult.BLOCKED
        if not persisted:
            if after != current or decision_result is not AutoOfferResult.WAITING:
                raise HostAutoOfferIntegrationError("result_unknown_recovery_invalid")
            return AutoOfferResult.RESULT_UNKNOWN
        snapshot = after.snapshot
        if (
            decision_result is not AutoOfferResult.WAITING
            or after.revision != current.revision + 1
            or snapshot.delivery_status is not DeliveryStatus.OFFER_SENT
            or snapshot.delivery_error is not None
            or snapshot.offer_attempted_at is None
            or snapshot.offer_sent_at is None
            or snapshot.steam_tradeoffer_id is None
            or snapshot.purchase_id != current.snapshot.purchase_id
            or snapshot.buff_order_id != current.snapshot.buff_order_id
            or snapshot.account_id != current.snapshot.account_id
            or snapshot.recipient_steam_id
            != current.snapshot.recipient_steam_id
        ):
            raise HostAutoOfferIntegrationError("result_unknown_recovery_invalid")
        return AutoOfferResult.WAITING

    def _step_confirmation_result_unknown_readonly_once(
        self,
        current: StoredDelivery,
    ) -> AutoOfferResult:
        result = self._bridge.recover_confirmation_result_unknown_readonly(current)
        after = getattr(result, "after", None)
        persisted = getattr(result, "persisted", None)
        decision = getattr(result, "decision", None)
        decision_result = getattr(decision, "result", None)
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
        ):
            raise HostAutoOfferIntegrationError(
                "confirmation_result_unknown_recovery_invalid"
            )
        if decision_result is AutoOfferResult.BLOCKED:
            if persisted or after != current:
                raise HostAutoOfferIntegrationError(
                    "confirmation_result_unknown_recovery_invalid"
                )
            return AutoOfferResult.BLOCKED
        if not persisted:
            if after != current or decision_result is not AutoOfferResult.WAITING:
                raise HostAutoOfferIntegrationError(
                    "confirmation_result_unknown_recovery_invalid"
                )
            return AutoOfferResult.RESULT_UNKNOWN
        snapshot = after.snapshot
        if (
            decision_result is not AutoOfferResult.WAITING
            or after.revision != current.revision + 1
            or snapshot.delivery_status not in {
                DeliveryStatus.OFFER_CONFIRMED,
                DeliveryStatus.OFFER_TERMINATED,
            }
            or snapshot.steam_tradeoffer_id
            != current.snapshot.steam_tradeoffer_id
            or snapshot.counterparty_steam_id
            != current.snapshot.counterparty_steam_id
            or snapshot.purchase_id != current.snapshot.purchase_id
            or snapshot.buff_order_id != current.snapshot.buff_order_id
            or snapshot.account_id != current.snapshot.account_id
            or snapshot.recipient_steam_id
            != current.snapshot.recipient_steam_id
            or (
                snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
                and snapshot.delivery_error is not None
            )
        ):
            raise HostAutoOfferIntegrationError(
                "confirmation_result_unknown_recovery_invalid"
            )
        return AutoOfferResult.WAITING

    def _step_accept_result_unknown_readonly_once(
        self,
        current: StoredDelivery,
    ) -> AutoOfferResult:
        result = self._bridge.recover_accept_result_unknown_readonly(current)
        after = getattr(result, "after", None)
        persisted = getattr(result, "persisted", None)
        decision = getattr(result, "decision", None)
        decision_result = getattr(decision, "result", None)
        if (
            type(after) is not StoredDelivery
            or type(persisted) is not bool
            or type(decision_result) is not AutoOfferResult
        ):
            raise HostAutoOfferIntegrationError(
                "accept_result_unknown_recovery_invalid"
            )
        if decision_result is AutoOfferResult.BLOCKED:
            if persisted or after != current:
                raise HostAutoOfferIntegrationError(
                    "accept_result_unknown_recovery_invalid"
                )
            return AutoOfferResult.BLOCKED
        if not persisted:
            if after != current or decision_result is not AutoOfferResult.WAITING:
                raise HostAutoOfferIntegrationError(
                    "accept_result_unknown_recovery_invalid"
                )
            return AutoOfferResult.RESULT_UNKNOWN
        snapshot = after.snapshot
        if (
            decision_result is not AutoOfferResult.WAITING
            or after.revision != current.revision + 1
            or snapshot.delivery_status not in {
                DeliveryStatus.AWAITING_INVENTORY,
                DeliveryStatus.OFFER_TERMINATED,
            }
            or snapshot.steam_tradeoffer_id
            != current.snapshot.steam_tradeoffer_id
            or snapshot.counterparty_steam_id
            != current.snapshot.counterparty_steam_id
            or snapshot.purchase_id != current.snapshot.purchase_id
            or snapshot.buff_order_id != current.snapshot.buff_order_id
            or snapshot.account_id != current.snapshot.account_id
            or snapshot.recipient_steam_id
            != current.snapshot.recipient_steam_id
            or (
                snapshot.delivery_status is DeliveryStatus.AWAITING_INVENTORY
                and snapshot.delivery_error is not None
            )
            or (
                snapshot.delivery_status is DeliveryStatus.OFFER_TERMINATED
                and snapshot.delivery_error != "offer_terminated"
            )
        ):
            raise HostAutoOfferIntegrationError(
                "accept_result_unknown_recovery_invalid"
            )
        return AutoOfferResult.WAITING

    def _run_result_unknown_recovery_tick(
        self,
        recoverable: Mapping[str, StoredDelivery],
        *,
        cursor: str | None,
    ) -> DeliveryTickOutcome:
        unknown_order_ids = {
            order_id
            for order_id, stored in recoverable.items()
            if stored.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
        }
        eligible = {
            order_id: stored
            for order_id, stored in recoverable.items()
            if self._is_c2a_result_unknown(stored)
            or self._is_c2b_result_unknown(stored)
            or self._is_c3_accept_result_unknown(stored)
        }
        if set(eligible) != unknown_order_ids:
            return DeliveryTickOutcome(AutoOfferResult.RESULT_UNKNOWN, cursor, ())
        visited: list[str] = []
        for order_id in self._visit_order_ids(tuple(eligible), cursor):
            visited.append(order_id)
            stored = eligible[order_id]
            if self._is_c2a_result_unknown(stored):
                result = self._step_result_unknown_readonly_once(stored)
            elif self._is_c2b_result_unknown(stored):
                result = self._step_confirmation_result_unknown_readonly_once(stored)
            else:
                result = self._step_accept_result_unknown_readonly_once(stored)
            if result is AutoOfferResult.BLOCKED:
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id,
                    tuple(visited),
                )
        refreshed = self._recoverable_by_order()
        outcome = (
            AutoOfferResult.RESULT_UNKNOWN
            if any(
                stored.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
                for stored in refreshed.values()
            )
            else AutoOfferResult.WAITING
        )
        return DeliveryTickOutcome(
            outcome,
            visited[-1] if visited else cursor,
            tuple(visited),
        )

    def _normal_admission_result(self, host_purchases: object) -> AutoOfferResult:
        self._require_runtime_identity()
        host_pending = self._host_pending_by_order(host_purchases)
        recoverable = self._recoverable_by_order()
        recoverable = {
            order_id: self._validate_normal_store_state(stored, order_id)
            for order_id, stored in recoverable.items()
        }
        stored_by_order = self._normal_store_by_order(host_pending, recoverable)
        if not self._normal_sets_match(host_pending, stored_by_order):
            return AutoOfferResult.BLOCKED
        if any(
            stored.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
            for stored in stored_by_order.values()
        ):
            return AutoOfferResult.RESULT_UNKNOWN
        return (
            AutoOfferResult.WAITING
            if stored_by_order
            else AutoOfferResult.COMPLETE
        )

    def _run_normal_delivery_tick(
        self,
        host_purchases: object,
        *,
        cursor: str | None,
    ) -> DeliveryTickOutcome:
        self._require_runtime_identity()
        host_pending = self._host_pending_by_order(host_purchases)
        recoverable = self._recoverable_by_order()
        recoverable = {
            order_id: self._validate_normal_store_state(stored, order_id)
            for order_id, stored in recoverable.items()
        }
        stored_by_order = self._normal_store_by_order(host_pending, recoverable)
        if not self._normal_sets_match(host_pending, stored_by_order):
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())
        if any(
            stored.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
            for stored in stored_by_order.values()
        ):
            if not self._checkout_is_resolved():
                return DeliveryTickOutcome(AutoOfferResult.RESULT_UNKNOWN, cursor, ())
            return self._run_result_unknown_recovery_tick(
                stored_by_order,
                cursor=cursor,
            )

        policies = {
            order_id: self._normal_delivery_policy(stored)
            for order_id, stored in stored_by_order.items()
        }
        if any(policy == "block" for policy in policies.values()):
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())
        if not stored_by_order:
            return DeliveryTickOutcome(AutoOfferResult.COMPLETE, None, ())
        if not self._checkout_is_resolved():
            return DeliveryTickOutcome(AutoOfferResult.WAITING, cursor, ())

        visited: list[str] = []
        for order_id in self._visit_order_ids(tuple(stored_by_order), cursor):
            visited.append(order_id)
            try:
                policy = policies[order_id]
                if policy == "receipt":
                    self._write_back_received(
                        host_pending[order_id],
                        stored_by_order[order_id],
                    )
                elif policy == "cleanup":
                    result = self._step_refund_cleanup_once(
                        host_purchases,
                        stored_by_order[order_id],
                    )
                    return DeliveryTickOutcome(
                        result,
                        order_id,
                        tuple(visited),
                    )
                elif policy == "read":
                    result = self._step_normal_read_once(stored_by_order[order_id])
                    if result in {
                        AutoOfferResult.BLOCKED,
                        AutoOfferResult.RESULT_UNKNOWN,
                    }:
                        return DeliveryTickOutcome(result, order_id, tuple(visited))
                elif policy == "send":
                    result = self._step_normal_send_once(
                        host_pending[order_id],
                        stored_by_order[order_id],
                    )
                    return DeliveryTickOutcome(result, order_id, tuple(visited))
                elif policy == "confirm":
                    result = self._step_normal_confirm_once(
                        host_pending[order_id],
                        stored_by_order[order_id],
                    )
                    return DeliveryTickOutcome(result, order_id, tuple(visited))
                elif policy == "accept":
                    result = self._step_normal_accept_once(
                        host_pending[order_id],
                        stored_by_order[order_id],
                    )
                    return DeliveryTickOutcome(result, order_id, tuple(visited))
                elif policy != "wait":
                    raise HostAutoOfferIntegrationError("delivery_tick_policy_invalid")
            except Exception:
                return DeliveryTickOutcome(
                    AutoOfferResult.BLOCKED,
                    order_id,
                    tuple(visited),
                )
        return DeliveryTickOutcome(
            AutoOfferResult.WAITING,
            visited[-1],
            tuple(visited),
        )

    def _next_purchase_result_canary(self, host_purchases: object) -> AutoOfferResult:
        if self._canary_completed:
            return AutoOfferResult.COMPLETE
        self._require_runtime_identity()
        host_pending = self._host_pending_by_order(host_purchases)
        recoverable = self._recoverable_by_order()
        recoverable = self._prepare_canary_before_dispatch(host_pending, recoverable)
        target = self._canary_target_order()
        if (
            recoverable
            and recoverable[target].snapshot.delivery_status
            is DeliveryStatus.RESULT_UNKNOWN
        ):
            return AutoOfferResult.RESULT_UNKNOWN
        if not recoverable:
            self._sync_canary_terminal_received(host_pending, recoverable)
            if not host_pending:
                if self._canary_owner_session is None:
                    raise HostAutoOfferIntegrationError("canary_owner_session_required")
                self._canary_owner_session.mark_completed()
                self._canary_completed = True
                return AutoOfferResult.COMPLETE
            return AutoOfferResult.BLOCKED
        self._validate_canary_current_sets(host_pending, recoverable)

        deferred_fresh = self._dispatch_fresh_deliveries()
        host_pending = self._host_pending_by_order(host_purchases)
        recoverable = self._recoverable_by_order()
        if not recoverable:
            self._sync_canary_terminal_received(host_pending, recoverable)
            if not host_pending:
                if self._canary_owner_session is None:
                    raise HostAutoOfferIntegrationError("canary_owner_session_required")
                self._canary_owner_session.mark_completed()
                self._canary_completed = True
                return AutoOfferResult.COMPLETE
            return AutoOfferResult.BLOCKED
        self._validate_canary_current_sets(host_pending, recoverable)
        if recoverable[target].snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            return AutoOfferResult.RESULT_UNKNOWN
        self._sync_canary_terminal_received(host_pending, recoverable)
        if not host_pending:
            if self._canary_owner_session is None:
                raise HostAutoOfferIntegrationError("canary_owner_session_required")
            self._canary_owner_session.mark_completed()
            self._canary_completed = True
            return AutoOfferResult.COMPLETE
        if set(host_pending) != set(recoverable):
            return AutoOfferResult.BLOCKED

        stored = recoverable[target]
        if target not in deferred_fresh:
            result, current = self._recover_canary_persisted_delivery(stored)
            if result in {
                AutoOfferResult.BLOCKED,
                AutoOfferResult.RESULT_UNKNOWN,
            }:
                return result
            if current.snapshot.delivery_status is DeliveryStatus.RECEIVED:
                self._write_back_received(host_pending[target], current)
                del host_pending[target]

        refreshed = self._recoverable_by_order()
        if host_pending:
            if set(host_pending) != {target} or set(refreshed) != {target}:
                return AutoOfferResult.BLOCKED
            return AutoOfferResult.WAITING
        if refreshed:
            return AutoOfferResult.BLOCKED
        if self._canary_owner_session is None:
            raise HostAutoOfferIntegrationError("canary_owner_session_required")
        self._canary_owner_session.mark_completed()
        self._canary_completed = True
        return AutoOfferResult.COMPLETE

    def next_purchase_result(self, host_purchases: object) -> AutoOfferResult:
        try:
            if self.is_canary:
                if self._canary_owner_session is None:
                    return AutoOfferResult.BLOCKED
                guard = self._canary_owner_session.runtime_guard()
            else:
                guard = self._canary_authority.runtime_guard()
            with guard:
                if self.is_canary:
                    return self._next_purchase_result_canary(host_purchases)
                return self._normal_admission_result(host_purchases)
        except (HostAutoOfferIntegrationError, CanaryAuthorityError):
            return AutoOfferResult.BLOCKED
        except Exception:
            return AutoOfferResult.BLOCKED

    def run_delivery_tick(
        self,
        host_purchases: object,
        *,
        cursor: str | None = None,
    ) -> DeliveryTickOutcome:
        """Visit at most eight exact normal deliveries with one action each."""

        if self.is_canary or (cursor is not None and _exact_order_id(cursor) is None):
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())
        try:
            with self._canary_authority.runtime_guard():
                return self._run_normal_delivery_tick(
                    host_purchases,
                    cursor=cursor,
                )
        except (HostAutoOfferIntegrationError, CanaryAuthorityError):
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())
        except Exception:
            return DeliveryTickOutcome(AutoOfferResult.BLOCKED, cursor, ())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_error: Exception | None = None
        try:
            self._bridge.close()
        except Exception as exc:
            close_error = exc
        finally:
            if self.is_canary and self._canary_owner_session is not None:
                try:
                    self._canary_owner_session.release_keep_fence()
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
        if close_error is not None:
            raise HostAutoOfferIntegrationError("auto_offer_close_failed") from close_error


def build_host_auto_offer_integration(
    *,
    config: Mapping[str, object] | None,
    buff_client,
    complete_purchase_receipt_by_id=None,
    delete_refund_cleanup_purchase=None,
    canary_permit: CanaryPermit | None = None,
    canary_authority: CanaryAuthority | None = None,
    runtime_state: AutoOfferRuntimeState | None = None,
) -> HostAutoOfferIntegration | None:
    config_enabled = is_auto_offer_enabled(config)
    registration_enabled = True
    if runtime_state is None:
        if not config_enabled:
            return None
    else:
        if type(runtime_state) is not AutoOfferRuntimeState:
            raise HostAutoOfferIntegrationError("runtime_state_invalid")
        if runtime_state.requested_enabled is not config_enabled:
            raise HostAutoOfferIntegrationError("runtime_state_config_mismatch")
        if runtime_state.mode is AutoOfferRuntimeMode.ON:
            if not runtime_state.requested_enabled:
                raise HostAutoOfferIntegrationError("runtime_state_invalid")
        elif runtime_state.mode is AutoOfferRuntimeMode.DRAINING:
            if runtime_state.requested_enabled or runtime_state.active_delivery_count <= 0:
                raise HostAutoOfferIntegrationError("runtime_state_invalid")
            registration_enabled = False
        else:
            raise HostAutoOfferIntegrationError("runtime_state_mode_not_buildable")
    if canary_authority is not None:
        raise HostAutoOfferIntegrationError("canary_authority_injection_forbidden")
    authority = get_canary_authority()
    if type(authority) is not CanaryAuthority:
        raise HostAutoOfferIntegrationError("canary_authority_invalid")
    account_id, account_steam_id = _exact_current_account()
    if not callable(complete_purchase_receipt_by_id):
        raise HostAutoOfferIntegrationError("receipt_writer_required")
    if canary_permit is None and not callable(delete_refund_cleanup_purchase):
        raise HostAutoOfferIntegrationError("refund_cleanup_writer_required")
    owner_session = None
    if canary_permit is not None:
        if type(canary_permit) is not CanaryPermit:
            raise HostAutoOfferIntegrationError("canary_permit_invalid")
        if (
            canary_permit.account_id != account_id
            or canary_permit.recipient_steam_id != _canonical_steam_id(account_steam_id)
        ):
            raise HostAutoOfferIntegrationError("canary_runtime_identity_mismatch")
        try:
            owner_session = authority._arm_owner_session(canary_permit)
        except CanaryAuthorityError as exc:
            raise HostAutoOfferIntegrationError("canary_authority_arm_failed") from exc
    try:
        bridge = _build_active_host_auto_offer_bridge(
            buff_client=buff_client,
            account_id=account_id,
            account_steam_id=account_steam_id,
            store_path=_STORE_PATH,
            canary_authority=authority,
            canary_owner_session=owner_session,
            canary_permit=canary_permit,
        )
    except Exception as exc:
        if owner_session is not None:
            owner_session.release_keep_fence()
        raise HostAutoOfferIntegrationError(
            "auto_offer_bridge_build_failed"
        ) from exc
    return HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=complete_purchase_receipt_by_id,
        delete_refund_cleanup_purchase=delete_refund_cleanup_purchase,
        canary_permit=canary_permit,
        canary_owner_session=owner_session,
        registration_enabled=registration_enabled,
    )


__all__ = [
    "DeliveryTickOutcome",
    "HostAutoOfferIntegration",
    "HostAutoOfferIntegrationError",
    "HostRecoveryOnlyMaintenance",
    "MAX_DELIVERY_ORDERS_PER_TICK",
    "build_host_auto_offer_integration",
    "build_host_recovery_only_maintenance",
    "is_auto_offer_enabled",
    "preflight_canary_permit",
]
