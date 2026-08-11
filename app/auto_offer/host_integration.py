"""Host-owned lifecycle for explicitly enabled native Auto Offer execution.

The host integration remains default-off.  When enabled it owns one local Store,
one Coordinator registry, one Steam read session, and the TASK-025 buyer-send
adapter.  Platform work is synchronous and bounded; no worker, retry loop,
scheduler, or background Auto Offer executor is created here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import requests

from app.accounts import get_account, get_current_id
from app.auto_offer.adapters import PlatformCapability
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.coordinator import DeliveryCoordinator
from app.auto_offer.platform_readonly import (
    BuffReadOnlyAdapter,
    SteamCompletedTradeReadOnlyAdapter,
    SteamTradeOfferReadOnlyAdapter,
)
from app.auto_offer.platform_write import BuffBuyerSendOfferAdapter
from app.auto_offer.steam_readonly_transport import (
    SteamCompletedTradeHttpReader,
    SteamTradeOfferHttpReader,
)
from app.auto_offer.store import AutoOfferStore, StoredDelivery
from app.config_loader import get_steam_credentials
from app.services.buff_checkout_guard import get_unresolved_checkout


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"
_TIMEOUT_SECONDS = 15.0


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


def _steam_cookie_for_expected(expected_steam_id: str) -> str:
    expected = _canonical_steam_id(expected_steam_id)
    credentials = get_steam_credentials()
    if not isinstance(credentials, Mapping):
        raise HostAutoOfferIntegrationError("steam_credentials_invalid")
    actual = _canonical_steam_id(credentials.get("steam_id"))
    if actual != expected:
        raise HostAutoOfferIntegrationError("steam_identity_mismatch")
    cookies = credentials.get("cookies")
    if type(cookies) is not str or not cookies:
        raise HostAutoOfferIntegrationError("steam_cookie_required")
    return cookies


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

    def step(self, delivery: StoredDelivery):
        self._require_open()
        try:
            return self._coordinator.step(delivery)
        except Exception:
            raise HostAutoOfferIntegrationError("auto_offer_step_failed") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        store_ok = _safe_close_store(self._store)
        session_ok = _safe_close_session(self._session)
        if not store_ok or not session_ok:
            raise HostAutoOfferIntegrationError("bridge_close_failed")


def _build_active_host_auto_offer_bridge(
    *,
    buff_client,
    account_id: str,
    account_steam_id: str,
    store_path: str | Path = _STORE_PATH,
) -> _ActiveHostAutoOfferBridge:
    """Build the one active read+SEND coordinator without platform I/O."""

    account = account_id
    if type(account) is not str or not account or account.strip() != account:
        raise HostAutoOfferIntegrationError("current_account_id_invalid")
    recipient = _canonical_steam_id(account_steam_id)
    cookie_string = _steam_cookie_for_expected(recipient)

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
        if (
            trade_offer_reader.bound_account_steam_id != recipient
            or completed_trade_reader.bound_account_steam_id != recipient
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
        adapters = {
            PlatformCapability.READ_DELIVERY_DIRECTION: buff_adapter,
            PlatformCapability.READ_OFFER_STATE: buff_adapter,
            PlatformCapability.READ_STEAM_TRADE_OFFER: trade_offer_adapter,
            PlatformCapability.READ_STEAM_COMPLETED_TRADE: completed_trade_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        }
        coordinator = DeliveryCoordinator(
            store,
            adapters,
            timeout_seconds=_TIMEOUT_SECONDS,
            allow_writes=True,
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


class HostAutoOfferIntegration:
    """Host-owned lifecycle, first-send authorization, and purchase gate seam."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self._account_id = bridge.account_id
        self._recipient_steam_id = bridge.recipient_steam_id
        self._fresh_deliveries: list[StoredDelivery] = []
        self._closed = False

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    def register_committed_purchase(self, purchase: Mapping[str, object]) -> None:
        try:
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
            self._fresh_deliveries.append(fresh)

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

    def _dispatch_fresh_deliveries(self) -> None:
        if not self._fresh_deliveries or not self._checkout_is_resolved():
            return
        self._require_runtime_identity()

        # Consume the authorization tokens before any platform step.  If any
        # step becomes ambiguous or raises, this run never retries those sends.
        fresh = tuple(self._fresh_deliveries)
        self._fresh_deliveries.clear()

        for delivery in fresh:
            direction_step = self._bridge.step(delivery)
            current = getattr(direction_step, "after", None)
            if type(current) is not StoredDelivery:
                raise HostAutoOfferIntegrationError("direction_step_invalid")

            snapshot = current.snapshot
            if snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION:
                return
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
                recovery_step = self._bridge.step(current)
                current = getattr(recovery_step, "after", None)
                if type(current) is not StoredDelivery:
                    raise HostAutoOfferIntegrationError("recovery_step_invalid")
                if current.snapshot.delivery_status is not DeliveryStatus.OFFER_SENT:
                    return
            elif current.snapshot.delivery_status is not DeliveryStatus.OFFER_SENT:
                raise HostAutoOfferIntegrationError("send_step_invalid")

    def next_purchase_result(self, host_purchases: object) -> AutoOfferResult:
        """Dispatch fresh first-send work, then gate the next host purchase."""

        try:
            self._dispatch_fresh_deliveries()
            self._require_runtime_identity()

            if not isinstance(host_purchases, list):
                return AutoOfferResult.BLOCKED
            host_order_ids: set[str] = set()
            for purchase in host_purchases:
                if not isinstance(purchase, Mapping):
                    return AutoOfferResult.BLOCKED
                if purchase.get("pending_receipt") is not True:
                    continue
                assetid = purchase.get("assetid")
                if assetid not in (None, ""):
                    if not isinstance(assetid, str) or not assetid.strip():
                        return AutoOfferResult.BLOCKED
                    continue
                order_id = _exact_order_id(purchase.get("buff_order_id"))
                if order_id is None or order_id in host_order_ids:
                    return AutoOfferResult.BLOCKED
                host_order_ids.add(order_id)

            store_order_ids: set[str] = set()
            for stored_delivery in self._bridge.list_recoverable():
                snapshot = stored_delivery.snapshot
                if (
                    snapshot.account_id != self._account_id
                    or snapshot.recipient_steam_id != self._recipient_steam_id
                ):
                    return AutoOfferResult.BLOCKED
                order_id = _exact_order_id(snapshot.buff_order_id)
                if order_id is None or order_id in store_order_ids:
                    return AutoOfferResult.BLOCKED
                store_order_ids.add(order_id)

            if host_order_ids != store_order_ids:
                return AutoOfferResult.BLOCKED
            if not host_order_ids:
                return AutoOfferResult.COMPLETE
            return AutoOfferResult.WAITING
        except HostAutoOfferIntegrationError:
            return AutoOfferResult.BLOCKED
        except Exception:
            return AutoOfferResult.BLOCKED

    def close(self) -> None:
        if self._closed:
            return
        dispatch_error: Exception | None = None
        try:
            self._dispatch_fresh_deliveries()
        except Exception as exc:
            dispatch_error = exc
        self._closed = True
        try:
            self._bridge.close()
        except Exception as exc:
            if dispatch_error is None:
                dispatch_error = exc
        if dispatch_error is not None:
            raise HostAutoOfferIntegrationError("auto_offer_close_failed") from dispatch_error


def build_host_auto_offer_integration(
    *,
    config: Mapping[str, object] | None,
    buff_client,
) -> HostAutoOfferIntegration | None:
    """Build the active bridge only for an explicitly enabled validated run."""

    if not is_auto_offer_enabled(config):
        return None

    account_id, account_steam_id = _exact_current_account()
    try:
        bridge = _build_active_host_auto_offer_bridge(
            buff_client=buff_client,
            account_id=account_id,
            account_steam_id=account_steam_id,
            store_path=_STORE_PATH,
        )
    except Exception as exc:
        raise HostAutoOfferIntegrationError(
            "auto_offer_bridge_build_failed"
        ) from exc
    return HostAutoOfferIntegration(bridge)


__all__ = [
    "HostAutoOfferIntegration",
    "HostAutoOfferIntegrationError",
    "build_host_auto_offer_integration",
    "is_auto_offer_enabled",
]
