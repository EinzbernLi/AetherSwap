from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.auto_offer.canary_authority as authority_module
import app.auto_offer.host_integration as host_integration
from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.canary_authority import CanaryAuthority, CanaryPermit
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
)
from app.auto_offer.store import StoredDelivery


ACCOUNT_ID = "account-1"
RECIPIENT_STEAM_ID = "76561198000000001"
COUNTERPARTY_STEAM_ID = "76561198000000002"
ORDER_ID = "order-task037-r1"
PURCHASE_ID = f"buff:{ORDER_ID}"
TRADEOFFER_ID = "offer-task037-r1"


def _delivery(
    status: DeliveryStatus,
    *,
    revision: int,
    tradeoffer_id: str | None = None,
) -> StoredDelivery:
    mode = None if status is DeliveryStatus.PENDING_DIRECTION else DeliveryMode.BUYER_SENDS_OFFER
    attempted_at = None
    sent_at = None
    if status in {
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    }:
        attempted_at = 1.0
    if status in {
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
    }:
        sent_at = 2.0
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=tradeoffer_id,
            offer_attempted_at=attempted_at,
            offer_sent_at=sent_at,
            received_at=None,
            delivery_error=(
                "write_result_unknown" if status is DeliveryStatus.RESULT_UNKNOWN else None
            ),
            pending_receipt=True,
            assetid=None,
        ),
        revision,
    )


def _host_row():
    return {
        "_db_id": 7,
        "buff_order_id": ORDER_ID,
        "pending_receipt": True,
        "assetid": None,
    }


def _permit(expected: StoredDelivery | None = None) -> CanaryPermit:
    return CanaryPermit(
        permit_id="permit-task037-r1",
        owner_nonce="owner-task037-r1",
        host_db_id=7,
        buff_order_id=ORDER_ID,
        purchase_id=PURCHASE_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=RECIPIENT_STEAM_ID,
        expected_counterparty_steam_id=COUNTERPARTY_STEAM_ID,
        expected_is_our_offer=True,
        expected_host_order_ids=(ORDER_ID,),
        expected_store_present=expected is not None,
        expected_store_revision=None if expected is None else expected.revision,
        expected_store_status=(
            None if expected is None else expected.snapshot.delivery_status.value
        ),
        expected_store_tradeoffer_id=(
            None if expected is None else expected.snapshot.steam_tradeoffer_id
        ),
        created_at=1.0,
    )


def _install_canary(monkeypatch, tmp_path, bridge, permit):
    authority = CanaryAuthority(_root=tmp_path / "authority")
    owner_session = authority._arm_owner_session(permit)
    monkeypatch.setattr(authority_module, "_PRODUCTION_AUTHORITY", authority)
    monkeypatch.setattr(
        host_integration,
        "_exact_current_account",
        lambda: (ACCOUNT_ID, RECIPIENT_STEAM_ID),
    )
    monkeypatch.setattr(
        host_integration,
        "_steam_cookie_for_expected",
        lambda _steam_id: "fake-cookie",
    )
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    receipt_calls = []
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=(
            lambda *args: receipt_calls.append(args) or True
        ),
        canary_permit=permit,
        canary_owner_session=owner_session,
    )
    return integration, owner_session, receipt_calls


class FreshUnknownBridge:
    account_id = ACCOUNT_ID
    recipient_steam_id = RECIPIENT_STEAM_ID

    def __init__(self):
        self.current = None
        self.events = []

    def register_committed_purchase(self, _record):
        assert self.current is None
        self.current = _delivery(DeliveryStatus.PENDING_DIRECTION, revision=1)
        return self.current

    def list_recoverable(self):
        return () if self.current is None else (self.current,)

    def get_by_purchase_id(self, purchase_id):
        if purchase_id != PURCHASE_ID:
            return None
        return self.current

    def step(self, delivery):
        status = delivery.snapshot.delivery_status
        if status is DeliveryStatus.PENDING_DIRECTION:
            self.events.append("direction")
            self.current = StoredDelivery(
                replace(
                    delivery.snapshot,
                    delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
                    delivery_status=DeliveryStatus.AWAITING_OFFER,
                ),
                delivery.revision + 1,
            )
            return SimpleNamespace(after=self.current)
        if status is DeliveryStatus.AWAITING_OFFER:
            raise AssertionError("fresh buyer SEND must use explicit authority")
        if status is DeliveryStatus.OFFER_ATTEMPTED:
            self.events.append("realtime")
            request = PlatformRequest(
                purchase_id=PURCHASE_ID,
                buff_order_id=ORDER_ID,
                account_id=ACCOUNT_ID,
                recipient_steam_id=RECIPIENT_STEAM_ID,
                revision=delivery.revision,
                capability=PlatformCapability.READ_OFFER_STATE,
                timeout_seconds=1.0,
            )
            return SimpleNamespace(
                before=delivery,
                after=delivery,
                persisted=False,
                decision=SimpleNamespace(result=AutoOfferResult.WAITING),
                platform_result=PlatformResult(
                    request,
                    PlatformResultStatus.RESULT_UNKNOWN,
                    "order_not_proven",
                ),
            )
        if status is DeliveryStatus.RESULT_UNKNOWN:
            self.events.append("recovery")
            self.current = StoredDelivery(
                replace(
                    delivery.snapshot,
                    delivery_status=DeliveryStatus.OFFER_SENT,
                    steam_tradeoffer_id=TRADEOFFER_ID,
                    offer_sent_at=2.0,
                    delivery_error=None,
                ),
                delivery.revision + 1,
            )
            return SimpleNamespace(
                after=self.current,
                persisted=True,
                decision=SimpleNamespace(result=AutoOfferResult.WAITING),
            )
        raise AssertionError(f"unexpected step from {status}")

    def read_send_authority(self, delivery):
        assert delivery.snapshot.delivery_status is DeliveryStatus.AWAITING_OFFER
        return object()

    def send_offer_with_authority(self, delivery, proof):
        assert proof is not None
        self.events.append("send")
        self.current = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
                offer_attempted_at=1.0,
                delivery_error=None,
            ),
            delivery.revision + 1,
        )
        request = PlatformRequest(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            revision=self.current.revision,
            capability=PlatformCapability.SEND_OFFER,
            timeout_seconds=1.0,
        )
        return SimpleNamespace(
            before=delivery,
            attempted=self.current,
            after=self.current,
            platform_result=PlatformResult(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "offer_created_unproven",
            ),
        )

    def recover_result_unknown_readonly(self, delivery):
        self.events.append("recovery_read")
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def close(self):
        pass


class ConfirmationUnknownBridge:
    account_id = ACCOUNT_ID
    recipient_steam_id = RECIPIENT_STEAM_ID

    def __init__(self, current):
        self.current = current
        self.events = []

    def register_committed_purchase(self, _record):
        raise AssertionError("existing Store row must not be registered")

    def list_recoverable(self):
        return (self.current,)

    def get_by_purchase_id(self, purchase_id):
        return self.current if purchase_id == PURCHASE_ID else None

    def read_confirmation_state(self, delivery):
        self.events.append("read_confirmation")
        request = PlatformRequest(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            revision=delivery.revision,
            capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
            timeout_seconds=1.0,
            steam_tradeoffer_id=TRADEOFFER_ID,
        )
        item = TradeOfferItemEvidence(730, "2", "asset-1", 1)
        platform_result = PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            evidence=SteamTradeOfferEvidence(
                steam_tradeoffer_id=TRADEOFFER_ID,
                account_steam_id=RECIPIENT_STEAM_ID,
                counterparty_steam_id=COUNTERPARTY_STEAM_ID,
                is_our_offer=True,
                lifecycle=SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
                items_to_give=(),
                items_to_receive=(item,),
            ),
        )
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
            platform_result=platform_result,
        )

    def step(self, delivery):
        if delivery.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN:
            self.events.append("recovery")
            raise AssertionError("active canary must not recover RESULT_UNKNOWN")
        assert delivery.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
        self.events.append("confirm")
        attempted = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            ),
            delivery.revision + 1,
        )
        self.current = StoredDelivery(
            replace(
                attempted.snapshot,
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
            ),
            attempted.revision + 1,
        )
        return SimpleNamespace(
            attempted=attempted,
            after=self.current,
            platform_result=SimpleNamespace(
                status=PlatformResultStatus.RESULT_UNKNOWN
            ),
        )

    def close(self):
        pass


def test_preflight_rejects_result_unknown_target():
    current = _delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        revision=5,
        tradeoffer_id=TRADEOFFER_ID,
    )
    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="canary_result_unknown_ineligible",
    ):
        host_integration.preflight_canary_permit(
            host_purchases=[_host_row()],
            unresolved_checkout=None,
            recoverable_deliveries=(current,),
            target_stored=current,
            target_db_id=7,
            target_buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            expected_counterparty_steam_id=COUNTERPARTY_STEAM_ID,
            expected_is_our_offer=True,
            permit_id="permit-preflight",
            owner_nonce="owner-preflight",
            created_at=1.0,
        )


def test_canary_send_result_unknown_stops_without_active_recovery(monkeypatch, tmp_path):
    bridge = FreshUnknownBridge()
    permit = _permit()
    integration, owner_session, receipt_calls = _install_canary(
        monkeypatch, tmp_path, bridge, permit
    )

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.WAITING
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED
    assert bridge.events == ["direction", "send"]
    assert receipt_calls == []

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.WAITING
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED
    assert bridge.events == ["direction", "send", "realtime"]
    assert receipt_calls == []
    owner_session.release_keep_fence()


def test_canary_confirmation_result_unknown_stops_all_later_active_progression(
    monkeypatch,
    tmp_path,
):
    current = _delivery(
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        revision=5,
        tradeoffer_id=TRADEOFFER_ID,
    )
    bridge = ConfirmationUnknownBridge(current)
    permit = _permit(current)
    integration, owner_session, receipt_calls = _install_canary(
        monkeypatch, tmp_path, bridge, permit
    )

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.RESULT_UNKNOWN
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert bridge.events == ["read_confirmation", "confirm"]
    assert receipt_calls == []

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.RESULT_UNKNOWN
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert bridge.events == ["read_confirmation", "confirm"]
    assert receipt_calls == []
    owner_session.release_keep_fence()


def test_normal_result_unknown_uses_only_read_recovery_and_never_resends(
    monkeypatch,
):
    bridge = FreshUnknownBridge()
    bridge.current = _delivery(DeliveryStatus.RESULT_UNKNOWN, revision=4)
    integration = host_integration.HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *_args: True,
    )
    monkeypatch.setattr(
        host_integration,
        "_exact_current_account",
        lambda: (ACCOUNT_ID, RECIPIENT_STEAM_ID),
    )
    monkeypatch.setattr(
        host_integration,
        "_steam_cookie_for_expected",
        lambda _steam_id: "fake-cookie",
    )
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.RESULT_UNKNOWN
    first = integration.run_delivery_tick([_host_row()])
    second = integration.run_delivery_tick([_host_row()], cursor=first.next_cursor)

    assert first.result is AutoOfferResult.RESULT_UNKNOWN
    assert second.result is AutoOfferResult.RESULT_UNKNOWN
    assert first.visited_order_ids == second.visited_order_ids == (ORDER_ID,)
    assert integration._fresh_deliveries == []
    assert bridge.events == ["recovery_read", "recovery_read"]
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
