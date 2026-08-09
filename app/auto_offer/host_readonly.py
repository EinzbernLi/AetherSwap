"""Explicit, default-off host bridge for the reviewed Auto Offer read runtime.

The bridge owns only local construction resources.  It never reads host config,
selects an account, attaches to startup, schedules work, or performs a platform
write.  Every platform step remains an explicit one-shot caller action.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

import requests

from app.auto_offer import AUTO_OFFER_DEFAULT_ENABLED
from app.auto_offer.contracts import DeliverySnapshot, DeliveryStatus
from app.auto_offer.runtime_readonly import (
    READONLY_RUNTIME_CAPABILITIES,
    ReadOnlyAutoOfferRuntime,
    ReadOnlyRuntimeConfigurationError,
    build_readonly_auto_offer_runtime,
)
from app.auto_offer.store import AutoOfferStore, AutoOfferStoreError, StoredDelivery


class HostReadOnlyBridgeConfigurationError(RuntimeError):
    """Sanitized host-bridge construction or ownership failure."""


def _strict_account_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise HostReadOnlyBridgeConfigurationError("invalid_account_id")
    return value


def _canonical_positive_decimal(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        raise HostReadOnlyBridgeConfigurationError(f"invalid_{field}")
    number = int(value)
    if number <= 0 or str(number) != value:
        raise HostReadOnlyBridgeConfigurationError(f"invalid_{field}")
    return value


def _positive_timeout(value: object) -> float:
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise HostReadOnlyBridgeConfigurationError("invalid_timeout")
    return float(value)


def _validated_store_path(value: object) -> str | Path:
    if type(value) is str:
        if not value or value.strip() != value:
            raise HostReadOnlyBridgeConfigurationError("invalid_store_path")
        return value
    if isinstance(value, Path):
        return value
    raise HostReadOnlyBridgeConfigurationError("invalid_store_path")


def _validated_buff_client(value: object) -> object:
    if value is None:
        raise HostReadOnlyBridgeConfigurationError("buff_client_required")
    try:
        reader = getattr(value, "get_steam_trades")
    except Exception:
        raise HostReadOnlyBridgeConfigurationError("invalid_buff_readonly_dependency") from None
    if not callable(reader):
        raise HostReadOnlyBridgeConfigurationError("invalid_buff_readonly_dependency")
    return value


def _validated_steam_credentials(
    value: object,
    expected_steam_id: str,
) -> str:
    if not isinstance(value, Mapping):
        raise HostReadOnlyBridgeConfigurationError("invalid_steam_credentials")
    try:
        credential_steam_id = _canonical_positive_decimal(
            value.get("steam_id"),
            "credential_steam_id",
        )
        cookie_string = value.get("cookies")
    except HostReadOnlyBridgeConfigurationError:
        raise
    except Exception:
        raise HostReadOnlyBridgeConfigurationError("invalid_steam_credentials") from None
    if credential_steam_id != expected_steam_id:
        raise HostReadOnlyBridgeConfigurationError("steam_identity_mismatch")
    if type(cookie_string) is not str or not cookie_string:
        raise HostReadOnlyBridgeConfigurationError("steam_cookie_required")
    return cookie_string


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


class HostReadOnlyAutoOfferBridge:
    """Owned local bridge to one explicitly constructed read-only runtime."""

    __slots__ = (
        "_store",
        "_runtime",
        "_session",
        "_account_id",
        "_recipient_steam_id",
        "_closed",
    )

    def __init__(
        self,
        *,
        store: AutoOfferStore,
        runtime: ReadOnlyAutoOfferRuntime,
        session: object,
        account_id: str,
        recipient_steam_id: str,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._session = session
        self._account_id = account_id
        self._recipient_steam_id = recipient_steam_id
        self._closed = False

    def __repr__(self) -> str:
        return (
            "HostReadOnlyAutoOfferBridge("
            f"account_id={self._account_id!r}, "
            f"recipient_steam_id={self._recipient_steam_id!r}, "
            f"capabilities={len(READONLY_RUNTIME_CAPABILITIES)}, "
            f"closed={self._closed!r})"
        )

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    @property
    def capabilities(self):
        return READONLY_RUNTIME_CAPABILITIES

    def _require_open(self) -> None:
        if self._closed:
            raise HostReadOnlyBridgeConfigurationError("bridge_closed")

    def register_committed_purchase(self, record: object) -> StoredDelivery:
        """Register one already-durable host purchase without platform I/O."""

        self._require_open()
        if not isinstance(record, Mapping):
            raise HostReadOnlyBridgeConfigurationError("invalid_purchase_record")
        try:
            buff_order_id = record.get("buff_order_id")
            pending_receipt = record.get("pending_receipt")
            assetid = record.get("assetid")
        except Exception:
            raise HostReadOnlyBridgeConfigurationError("invalid_purchase_record") from None
        if (
            type(buff_order_id) is not str
            or not buff_order_id
            or buff_order_id.strip() != buff_order_id
        ):
            raise HostReadOnlyBridgeConfigurationError("invalid_buff_order_id")
        if pending_receipt is not True:
            raise HostReadOnlyBridgeConfigurationError("purchase_not_pending_receipt")
        if assetid not in (None, ""):
            raise HostReadOnlyBridgeConfigurationError("purchase_already_has_asset")

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
            return self._store.ensure_initial(snapshot)
        except Exception:
            raise HostReadOnlyBridgeConfigurationError("purchase_registration_failed") from None

    def list_recoverable(self) -> tuple[StoredDelivery, ...]:
        """Return the Store's deterministic recoverable set without stepping it."""

        self._require_open()
        try:
            return tuple(self._store.list_recoverable())
        except AutoOfferStoreError:
            raise HostReadOnlyBridgeConfigurationError("store_read_failed") from None

    def step(self, delivery: StoredDelivery):
        """Delegate exactly one caller-requested step to the reviewed runtime."""

        self._require_open()
        return self._runtime.step(delivery)

    def close(self) -> None:
        """Close owned local resources once; no platform request is performed."""

        if self._closed:
            return
        self._closed = True
        store_ok = _safe_close_store(self._store)
        session_ok = _safe_close_session(self._session)
        if not store_ok or not session_ok:
            raise HostReadOnlyBridgeConfigurationError("bridge_close_failed")


def build_host_readonly_auto_offer_bridge(
    *,
    enabled: bool = AUTO_OFFER_DEFAULT_ENABLED,
    buff_client: object | None = None,
    account_id: object = None,
    account_steam_id: object = None,
    steam_credentials: object = None,
    store_path: object = "config/auto_offer.db",
    timeout_seconds: float = 15.0,
) -> HostReadOnlyAutoOfferBridge | None:
    """Build the explicit host bridge without attaching it to host execution."""

    if type(enabled) is not bool:
        raise HostReadOnlyBridgeConfigurationError("enabled_must_be_bool")
    if enabled is False:
        return None

    account = _strict_account_id(account_id)
    host_steam_id = _canonical_positive_decimal(account_steam_id, "account_steam_id")
    cookie_string = _validated_steam_credentials(steam_credentials, host_steam_id)
    readonly_buff_client = _validated_buff_client(buff_client)
    path = _validated_store_path(store_path)
    timeout = _positive_timeout(timeout_seconds)

    session = None
    store = None
    try:
        session = requests.Session()
        if getattr(session, "verify", None) is False:
            raise HostReadOnlyBridgeConfigurationError("steam_tls_verification_disabled")
        if not callable(getattr(session, "get", None)) or not callable(
            getattr(session, "close", None)
        ):
            raise HostReadOnlyBridgeConfigurationError("invalid_steam_session")

        store = AutoOfferStore(path)
        store.initialize()
        runtime = build_readonly_auto_offer_runtime(
            enabled=True,
            store=store,
            buff_client=readonly_buff_client,
            account_id=account,
            recipient_steam_id=host_steam_id,
            steam_cookie_string=cookie_string,
            steam_session=session,
            timeout_seconds=timeout,
        )
        if type(runtime) is not ReadOnlyAutoOfferRuntime:
            raise HostReadOnlyBridgeConfigurationError("readonly_runtime_required")
    except HostReadOnlyBridgeConfigurationError:
        _safe_close_store(store)
        _safe_close_session(session)
        raise
    except ReadOnlyRuntimeConfigurationError:
        _safe_close_store(store)
        _safe_close_session(session)
        raise HostReadOnlyBridgeConfigurationError(
            "readonly_runtime_configuration_failed"
        ) from None
    except AutoOfferStoreError:
        _safe_close_store(store)
        _safe_close_session(session)
        raise HostReadOnlyBridgeConfigurationError("store_initialization_failed") from None
    except Exception:
        _safe_close_store(store)
        _safe_close_session(session)
        raise HostReadOnlyBridgeConfigurationError("host_bridge_construction_failed") from None

    return HostReadOnlyAutoOfferBridge(
        store=store,
        runtime=runtime,
        session=session,
        account_id=account,
        recipient_steam_id=host_steam_id,
    )


__all__ = [
    "HostReadOnlyAutoOfferBridge",
    "HostReadOnlyBridgeConfigurationError",
    "build_host_readonly_auto_offer_bridge",
]
