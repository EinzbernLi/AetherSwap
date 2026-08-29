from types import SimpleNamespace

import pytest

from app.auto_offer import host_integration
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from app.auto_offer.store import StoredDelivery


ACCOUNT_ID = "account-1"
RECIPIENT = "76561198000000001"
COUNTERPARTY = "76561198000000002"
ORDER_ID = "order-1"
OFFER_ID = "offer-1"


def _snapshot(
    status,
    *,
    tradeoffer_id=None,
    counterparty=None,
    offer_attempted_at=10.0,
    offer_sent_at=None,
    delivery_error=None,
):
    value = DeliverySnapshot(
        purchase_id=f"buff:{ORDER_ID}",
        buff_order_id=ORDER_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=RECIPIENT,
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=status,
        steam_tradeoffer_id=tradeoffer_id,
        offer_attempted_at=offer_attempted_at,
        offer_sent_at=offer_sent_at,
        received_at=None,
        delivery_error=delivery_error,
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id=counterparty,
    )
    validate_delivery_snapshot(value)
    return value


def _stored(status, revision, **kwargs):
    return StoredDelivery(_snapshot(status, **kwargs), revision)


def _host_row():
    return {
        "_db_id": 41,
        "buff_order_id": ORDER_ID,
        "pending_receipt": True,
        "assetid": None,
    }


class StagedRecoveryBridge:
    account_id = ACCOUNT_ID
    recipient_steam_id = RECIPIENT

    def __init__(self, current, *, malformed_recovery=False):
        self.current = current
        self.malformed_recovery = malformed_recovery
        self.recovery_calls = []
        self.step_calls = []
        self.closed = False

    def list_recoverable(self):
        return (self.current,)

    def get_by_purchase_id(self, purchase_id):
        assert purchase_id == f"buff:{ORDER_ID}"
        return self.current

    def recover_result_unknown_readonly(self, current):
        self.recovery_calls.append(current)
        if self.malformed_recovery:
            after = StoredDelivery(
                DeliverySnapshot(
                    purchase_id=current.snapshot.purchase_id,
                    buff_order_id=current.snapshot.buff_order_id,
                    account_id=current.snapshot.account_id,
                    recipient_steam_id=current.snapshot.recipient_steam_id,
                    delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
                    delivery_status=DeliveryStatus.OFFER_SENT,
                    steam_tradeoffer_id=None,
                    offer_attempted_at=10.0,
                    offer_sent_at=11.0,
                    received_at=None,
                    delivery_error=None,
                    pending_receipt=True,
                    assetid=None,
                    counterparty_steam_id=None,
                ),
                current.revision + 1,
            )
        else:
            after = _stored(
                DeliveryStatus.OFFER_SENT,
                current.revision + 1,
                tradeoffer_id=OFFER_ID,
                offer_sent_at=11.0,
                counterparty=None,
            )
        self.current = after
        return SimpleNamespace(
            after=after,
            persisted=True,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def step(self, current):
        self.step_calls.append(current)
        assert current.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
        assert current.snapshot.steam_tradeoffer_id == OFFER_ID
        assert current.snapshot.counterparty_steam_id is None
        after = _stored(
            DeliveryStatus.OFFER_CONFIRMED,
            current.revision + 1,
            tradeoffer_id=OFFER_ID,
            offer_sent_at=11.0,
            counterparty=COUNTERPARTY,
        )
        self.current = after
        return SimpleNamespace(
            after=after,
            persisted=True,
            decision=SimpleNamespace(result=AutoOfferResult.WAITING),
        )

    def close(self):
        self.closed = True


def _integration(monkeypatch, tmp_path, bridge):
    authority = host_integration.CanaryAuthority(_root=tmp_path / "authority")
    monkeypatch.setattr(host_integration, "get_canary_authority", lambda: authority)
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_require_runtime_identity",
        lambda self: None,
    )
    monkeypatch.setattr(
        host_integration.HostAutoOfferIntegration,
        "_checkout_is_resolved",
        staticmethod(lambda: True),
    )
    return host_integration.HostAutoOfferIntegration(bridge)


def test_result_unknown_buff_binding_stops_tick_before_exact_steam_read(
    monkeypatch,
    tmp_path,
):
    unknown = _stored(
        DeliveryStatus.RESULT_UNKNOWN,
        3,
        delivery_error="write_result_unknown",
    )
    bridge = StagedRecoveryBridge(unknown)
    integration = _integration(monkeypatch, tmp_path, bridge)

    outcome = integration.run_delivery_tick([_host_row()])

    assert outcome.result is AutoOfferResult.WAITING
    assert outcome.visited_order_ids == (ORDER_ID,)
    assert len(bridge.recovery_calls) == 1
    assert bridge.step_calls == []
    recovered = bridge.current.snapshot
    assert recovered.delivery_status is DeliveryStatus.OFFER_SENT
    assert recovered.steam_tradeoffer_id == OFFER_ID
    assert recovered.counterparty_steam_id is None


def test_next_tick_uses_exact_steam_step_and_binds_counterparty(
    monkeypatch,
    tmp_path,
):
    recovered = _stored(
        DeliveryStatus.OFFER_SENT,
        4,
        tradeoffer_id=OFFER_ID,
        offer_sent_at=11.0,
        counterparty=None,
    )
    bridge = StagedRecoveryBridge(recovered)
    integration = _integration(monkeypatch, tmp_path, bridge)

    outcome = integration.run_delivery_tick([_host_row()])

    assert outcome.result is AutoOfferResult.WAITING
    assert outcome.visited_order_ids == (ORDER_ID,)
    assert bridge.recovery_calls == []
    assert len(bridge.step_calls) == 1
    after = bridge.current.snapshot
    assert after.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert after.steam_tradeoffer_id == OFFER_ID
    assert after.counterparty_steam_id == COUNTERPARTY


def test_result_unknown_recovery_without_exact_offer_id_blocks(
    monkeypatch,
    tmp_path,
):
    unknown = _stored(
        DeliveryStatus.RESULT_UNKNOWN,
        3,
        delivery_error="write_result_unknown",
    )
    bridge = StagedRecoveryBridge(unknown, malformed_recovery=True)
    integration = _integration(monkeypatch, tmp_path, bridge)

    outcome = integration.run_delivery_tick([_host_row()])

    assert outcome.result is AutoOfferResult.BLOCKED
    assert outcome.visited_order_ids == ()
    assert len(bridge.recovery_calls) == 1
    assert bridge.step_calls == []


def test_recovery_maintenance_allows_counterparty_to_remain_unbound_only_at_offer_sent():
    offer_sent = _stored(
        DeliveryStatus.OFFER_SENT,
        4,
        tradeoffer_id=OFFER_ID,
        offer_sent_at=11.0,
        counterparty=None,
    )
    host_integration.HostRecoveryOnlyMaintenance._validate_continuation_target(
        offer_sent
    )

    confirmed = _stored(
        DeliveryStatus.OFFER_CONFIRMED,
        5,
        tradeoffer_id=OFFER_ID,
        offer_sent_at=11.0,
        counterparty=None,
    )
    with pytest.raises(
        host_integration.HostAutoOfferIntegrationError,
        match="maintenance_target_not_recoverable",
    ):
        host_integration.HostRecoveryOnlyMaintenance._validate_continuation_target(
            confirmed
        )
