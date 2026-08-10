"""Minimal host attachment for the read-only Auto Offer runtime.

This module owns only the host boundary required by TASK-022.  It does not
create a worker, scheduler, platform loop, or write-capable platform client.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.accounts import get_account, get_current_id
from app.auto_offer.contracts import AutoOfferResult
from app.auto_offer.host_readonly import (
    HostReadOnlyAutoOfferBridge,
    build_host_readonly_auto_offer_bridge,
)
from app.config_loader import get_steam_credentials


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"


class HostAutoOfferIntegrationError(RuntimeError):
    """Raised when the host cannot safely attach the read-only bridge."""


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


class HostAutoOfferIntegration:
    """Host-owned lifecycle and purchase-gate seam for one pipeline run."""

    def __init__(self, bridge: HostReadOnlyAutoOfferBridge) -> None:
        self._bridge = bridge
        self._account_id = bridge.account_id
        self._recipient_steam_id = bridge.recipient_steam_id
        self._closed = False

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def recipient_steam_id(self) -> str:
        return self._recipient_steam_id

    def register_committed_purchase(self, purchase: Mapping[str, object]) -> None:
        try:
            self._bridge.register_committed_purchase(dict(purchase))
        except Exception as exc:
            raise HostAutoOfferIntegrationError(
                "auto_offer_registration_failed"
            ) from exc

    def next_purchase_result(self, host_purchases: object) -> AutoOfferResult:
        """Return the historical result used to gate the next purchase."""

        try:
            current_id, current_steam_id = _exact_current_account()
            credentials = get_steam_credentials()
            if not isinstance(credentials, Mapping):
                return AutoOfferResult.BLOCKED
            credential_steam_id = credentials.get("steam_id")
            if credential_steam_id != current_steam_id:
                return AutoOfferResult.BLOCKED
            if current_id != self._account_id or current_steam_id != self._recipient_steam_id:
                return AutoOfferResult.BLOCKED

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
        except Exception:
            return AutoOfferResult.BLOCKED

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bridge.close()


def build_host_auto_offer_integration(
    *,
    config: Mapping[str, object] | None,
    buff_client,
) -> HostAutoOfferIntegration | None:
    """Build the bridge only for an explicitly enabled, validated config."""

    if not is_auto_offer_enabled(config):
        return None

    account_id, account_steam_id = _exact_current_account()
    credentials = get_steam_credentials()
    try:
        bridge = build_host_readonly_auto_offer_bridge(
            enabled=True,
            buff_client=buff_client,
            account_id=account_id,
            account_steam_id=account_steam_id,
            steam_credentials=credentials,
            store_path=_STORE_PATH,
        )
    except Exception as exc:
        raise HostAutoOfferIntegrationError(
            "auto_offer_bridge_build_failed"
        ) from exc
    if bridge is None:
        raise HostAutoOfferIntegrationError("auto_offer_bridge_missing")
    return HostAutoOfferIntegration(bridge)
