from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.auto_offer.canary_authority as authority_module
import app.auto_offer.host_integration as host_integration
from app.auto_offer.adapters import (
    ConfirmOfferEvidence,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.canary_authority import CanaryAuthority, CanaryAuthorityError, CanaryPermit
from app.auto_offer.contracts import AutoOfferResult, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.coordinator import DeliveryCoordinator, ReadOnlyCoordinatorBlockedError
from app.auto_offer.host_integration import HostAutoOfferIntegration, HostAutoOfferIntegrationError
from app.auto_offer.store import StoredDelivery

ACCOUNT_ID = "account-1"
RECIPIENT_STEAM_ID = "76561198000000001"
COUNTERPARTY_STEAM_ID = "76561198000000002"
WRONG_COUNTERPARTY_STEAM_ID = "76561198000000003"
ORDER_ID = "order-1"
PURCHASE_ID = f"buff:{ORDER_ID}"
TRADEOFFER_ID = "offer-order-1"


def _delivery(
    status: DeliveryStatus,
    *,
    revision: int = 5,
    tradeoffer_id: str | None = TRADEOFFER_ID,
) -> StoredDelivery:
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
    if status in {
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
    }:
        tradeoffer_id = None
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id=PURCHASE_ID,
            buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
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


def _request(delivery: StoredDelivery, capability=PlatformCapability.READ_STEAM_TRADE_OFFER):
    return PlatformRequest(
        purchase_id=delivery.snapshot.purchase_id,
        buff_order_id=delivery.snapshot.buff_order_id,
        account_id=delivery.snapshot.account_id,
        recipient_steam_id=delivery.snapshot.recipient_steam_id,
        revision=delivery.revision,
        capability=capability,
        timeout_seconds=1.0,
        steam_tradeoffer_id=delivery.snapshot.steam_tradeoffer_id,
    )


def _evidence(
    *,
    counterparty: str = COUNTERPARTY_STEAM_ID,
    is_our_offer: bool = True,
    lifecycle: SteamTradeOfferLifecycle = SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
    outgoing: bool = False,
) -> SteamTradeOfferEvidence:
    item = TradeOfferItemEvidence(730, "2", "asset-1", 1)
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id=TRADEOFFER_ID,
        account_steam_id=RECIPIENT_STEAM_ID,
        counterparty_steam_id=counterparty,
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=(item,) if outgoing else (),
        items_to_receive=(item,),
    )


class MemoryStore:
    def __init__(self, current: StoredDelivery):
        self.current = current
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id):
        return self.current if purchase_id == self.current.snapshot.purchase_id else None

    def advance(self, current, target):
        assert current == self.current
        self.advance_calls.append((current, target))
        self.current = StoredDelivery(target, current.revision + 1)
        return self.current


class Adapter:
    def __init__(self, capability, factory):
        self.capabilities = frozenset({capability})
        self._factory = factory
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return self._factory(request)


def _read_adapter(evidence):
    return Adapter(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            evidence=evidence,
        ),
    )


def _confirm_adapter():
    return Adapter(
        PlatformCapability.CONFIRM_OFFER,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            evidence=ConfirmOfferEvidence(
                steam_tradeoffer_id=request.steam_tradeoffer_id,
                account_steam_id=RECIPIENT_STEAM_ID,
            ),
        ),
    )


def _coordinator(current, read_evidence, *, expected=True):
    store = MemoryStore(current)
    read = _read_adapter(read_evidence)
    confirm = _confirm_adapter()
    kwargs = {}
    if expected:
        kwargs = {
            "expected_trade_offer_counterparty_steam_id": COUNTERPARTY_STEAM_ID,
            "expected_trade_offer_is_our_offer": True,
        }
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_STEAM_TRADE_OFFER: read,
            PlatformCapability.CONFIRM_OFFER: confirm,
        },
        timeout_seconds=1.0,
        allow_writes=True,
        allow_confirmation_writes=True,
        **kwargs,
    )
    return coordinator, store, read, confirm


@pytest.mark.parametrize(
    "read_evidence",
    [
        _evidence(counterparty=WRONG_COUNTERPARTY_STEAM_ID),
        _evidence(is_our_offer=False),
    ],
)
def test_offer_sent_wrong_counterparty_or_direction_cannot_persist_confirmation_required(read_evidence):
    current = _delivery(DeliveryStatus.OFFER_SENT)
    coordinator, store, read, confirm = _coordinator(current, read_evidence)

    result = coordinator.step(current)

    assert result.decision.result is AutoOfferResult.BLOCKED
    assert result.decision.detail == "identity_mismatch"
    assert result.persisted is False
    assert result.after == current
    assert store.advance_calls == []
    assert len(read.calls) == 1
    assert confirm.calls == []


def test_outgoing_items_remain_blocked_before_confirmation_eligibility():
    current = _delivery(DeliveryStatus.OFFER_SENT)
    coordinator, store, _read, confirm = _coordinator(
        current,
        _evidence(outgoing=True),
    )

    result = coordinator.step(current)

    assert result.decision.result is AutoOfferResult.BLOCKED
    assert result.decision.detail == "trade_offer_outgoing_items_present"
    assert result.persisted is False
    assert store.advance_calls == []
    assert confirm.calls == []


def test_exact_offer_sent_evidence_may_persist_confirmation_required_only_after_identity_match():
    current = _delivery(DeliveryStatus.OFFER_SENT)
    coordinator, store, read, confirm = _coordinator(current, _evidence())

    result = coordinator.step(current)

    assert result.persisted is True
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
    assert store.current == result.after
    assert len(read.calls) == 1
    assert confirm.calls == []


def test_direct_canary_confirmation_without_fresh_identity_proof_is_blocked_before_attempt():
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    coordinator, store, _read, confirm = _coordinator(current, _evidence())

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_identity_proof_required",
    ):
        coordinator.step(current)

    assert store.current == current
    assert store.advance_calls == []
    assert confirm.calls == []


@pytest.mark.parametrize(
    "bad_evidence",
    [
        _evidence(counterparty=WRONG_COUNTERPARTY_STEAM_ID),
        _evidence(is_our_offer=False),
    ],
)
def test_persisted_confirmation_required_wrong_identity_never_mints_proof_or_confirms(bad_evidence):
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    coordinator, store, read, confirm = _coordinator(current, bad_evidence)

    result = coordinator.read_confirmation_state(current)

    assert result.decision.result is AutoOfferResult.BLOCKED
    assert result.persisted is False
    assert store.current == current
    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_identity_proof_required",
    ):
        coordinator.step(current)
    assert len(read.calls) == 1
    assert confirm.calls == []
    assert store.advance_calls == []


def test_exact_created_needs_confirmation_read_mints_one_shot_proof_then_confirms_once():
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    coordinator, store, read, confirm = _coordinator(current, _evidence())

    read_result = coordinator.read_confirmation_state(current)
    assert read_result.persisted is False
    assert read_result.after == current
    assert read_result.decision.result is AutoOfferResult.WAITING

    confirm_result = coordinator.step(current)

    assert confirm_result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    assert confirm_result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert len(read.calls) == 1
    assert len(confirm.calls) == 1
    assert coordinator._confirmation_identity_proof is None
    assert [call[1].delivery_status for call in store.advance_calls] == [
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
    ]


def test_active_or_accepted_exact_read_recovers_without_confirmation_write():
    for lifecycle in (
        SteamTradeOfferLifecycle.ACTIVE,
        SteamTradeOfferLifecycle.ACCEPTED,
    ):
        current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
        coordinator, store, read, confirm = _coordinator(
            current,
            _evidence(lifecycle=lifecycle),
        )

        result = coordinator.read_confirmation_state(current)

        assert result.persisted is True
        assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
        assert store.current == result.after
        assert len(read.calls) == 1
        assert confirm.calls == []
        assert coordinator._confirmation_identity_proof is None


def test_normal_non_canary_confirmation_behavior_remains_backward_compatible():
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    coordinator, store, _read, confirm = _coordinator(
        current,
        _evidence(),
        expected=False,
    )

    result = coordinator.step(current)

    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert len(confirm.calls) == 1
    assert [call[1].delivery_status for call in store.advance_calls] == [
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_CONFIRMED,
    ]


def _permit(
    *,
    expected_store_status: DeliveryStatus = DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
) -> CanaryPermit:
    return CanaryPermit(
        permit_id="permit-task039",
        owner_nonce="owner-task039",
        host_db_id=7,
        buff_order_id=ORDER_ID,
        purchase_id=PURCHASE_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=RECIPIENT_STEAM_ID,
        expected_counterparty_steam_id=COUNTERPARTY_STEAM_ID,
        expected_is_our_offer=True,
        expected_host_order_ids=(ORDER_ID,),
        expected_store_present=True,
        expected_store_revision=5,
        expected_store_status=expected_store_status.value,
        expected_store_tradeoffer_id=TRADEOFFER_ID,
        created_at=1.0,
    )


def _host_row():
    return {
        "_db_id": 7,
        "buff_order_id": ORDER_ID,
        "pending_receipt": True,
        "assetid": None,
    }


def _read_platform_result(current, evidence, *, status=PlatformResultStatus.SUCCESS, detail=None):
    return PlatformResult(
        request=_request(current),
        status=status,
        detail=detail,
        evidence=evidence if status is PlatformResultStatus.SUCCESS else None,
    )


class HostConfirmationBridge:
    account_id = ACCOUNT_ID
    recipient_steam_id = RECIPIENT_STEAM_ID

    def __init__(self, current, *, read_result, confirm_status=PlatformResultStatus.SUCCESS):
        self.current = current
        self.read_result = read_result
        self.confirm_status = confirm_status
        self.events = []

    def list_recoverable(self):
        return (self.current,)

    def get_by_purchase_id(self, purchase_id):
        return self.current if purchase_id == PURCHASE_ID else None

    def register_committed_purchase(self, _record):
        raise AssertionError("existing canary Store row must not be registered")

    def read_confirmation_state(self, delivery):
        self.events.append("read")
        return self.read_result(delivery)

    def step(self, delivery):
        self.events.append("confirm")
        assert delivery.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
        attempted = StoredDelivery(
            replace(
                delivery.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            ),
            delivery.revision + 1,
        )
        if self.confirm_status is PlatformResultStatus.SUCCESS:
            after = StoredDelivery(
                replace(
                    attempted.snapshot,
                    delivery_status=DeliveryStatus.OFFER_CONFIRMED,
                ),
                attempted.revision + 1,
            )
        elif self.confirm_status is PlatformResultStatus.RESULT_UNKNOWN:
            after = StoredDelivery(
                replace(
                    attempted.snapshot,
                    delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                    delivery_error="write_result_unknown",
                ),
                attempted.revision + 1,
            )
        else:
            after = attempted
        self.current = after
        return SimpleNamespace(
            attempted=attempted,
            platform_result=SimpleNamespace(status=self.confirm_status),
            after=after,
        )

    def close(self):
        pass


def _install_canary(monkeypatch, tmp_path, bridge):
    authority = CanaryAuthority(_root=tmp_path / "authority")
    permit = _permit()
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
    integration = HostAutoOfferIntegration(
        bridge,
        complete_purchase_receipt_by_id=lambda *_args: True,
        canary_permit=permit,
        canary_owner_session=owner_session,
    )
    return integration, owner_session


def test_host_persisted_confirmation_required_wrong_identity_stops_before_confirm(monkeypatch, tmp_path):
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)

    def read_result(delivery):
        platform_result = PlatformResult(
            request=_request(delivery),
            status=PlatformResultStatus.FAILURE,
            detail="identity_mismatch",
        )
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.BLOCKED),
            platform_result=platform_result,
        )

    bridge = HostConfirmationBridge(current, read_result=read_result)
    integration, owner_session = _install_canary(monkeypatch, tmp_path, bridge)

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.BLOCKED
    assert bridge.events == ["read"]
    owner_session.release_keep_fence()


def test_host_persisted_confirmation_required_reads_exact_identity_before_confirm(monkeypatch, tmp_path):
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)

    def read_result(delivery):
        return SimpleNamespace(
            after=delivery,
            persisted=False,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
            platform_result=_read_platform_result(delivery, _evidence()),
        )

    bridge = HostConfirmationBridge(current, read_result=read_result)
    integration, owner_session = _install_canary(monkeypatch, tmp_path, bridge)

    assert integration.next_purchase_result([_host_row()]) is AutoOfferResult.WAITING
    assert bridge.events[:2] == ["read", "confirm"]
    assert bridge.current.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    owner_session.release_keep_fence()


def test_preflight_freezes_exact_counterparty_and_direction_and_rejects_invalid_values():
    current = _delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    permit = host_integration.preflight_canary_permit(
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
    assert permit.expected_counterparty_steam_id == COUNTERPARTY_STEAM_ID
    assert permit.expected_is_our_offer is True

    with pytest.raises(HostAutoOfferIntegrationError):
        host_integration.preflight_canary_permit(
            host_purchases=[_host_row()],
            unresolved_checkout=None,
            recoverable_deliveries=(current,),
            target_stored=current,
            target_db_id=7,
            target_buff_order_id=ORDER_ID,
            account_id=ACCOUNT_ID,
            recipient_steam_id=RECIPIENT_STEAM_ID,
            expected_counterparty_steam_id=RECIPIENT_STEAM_ID,
            expected_is_our_offer=True,
            permit_id="permit-bad",
            owner_nonce="owner-bad",
            created_at=1.0,
        )

    with pytest.raises(HostAutoOfferIntegrationError):
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
            expected_is_our_offer="true",
            permit_id="permit-bad-direction",
            owner_nonce="owner-bad-direction",
            created_at=1.0,
        )
