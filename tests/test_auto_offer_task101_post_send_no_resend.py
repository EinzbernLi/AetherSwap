from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    DeliveryDirectionEvidence,
    OfferStateEvidence,
    PlatformCapability,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from app.auto_offer.coordinator import (
    DeliveryCoordinator,
    ReadOnlyCoordinatorBlockedError,
)
from app.auto_offer.store import StoredDelivery


PURCHASE_ID = "buff:task101-order"
ORDER_ID = "task101-order"
ACCOUNT_ID = "task101-account"
RECIPIENT_STEAM_ID = "76561198000000101"
COUNTERPARTY_STEAM_ID = "76561198000000102"


class RecordingStore:
    def __init__(self, current: StoredDelivery):
        self.current = current
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id: str):
        assert purchase_id == self.current.snapshot.purchase_id
        return self.current

    def advance(self, current: StoredDelivery, target: DeliverySnapshot):
        assert current == self.current
        self.advance_calls.append((current, target))
        self.current = StoredDelivery(target, current.revision + 1)
        return self.current


class RecordingAdapter:
    def __init__(self, capability: PlatformCapability, factory):
        self.capabilities = frozenset({capability})
        self.factory = factory
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return self.factory(request)


def _delivery(
    status: DeliveryStatus,
    *,
    revision: int = 5,
) -> StoredDelivery:
    snapshot = DeliverySnapshot(
        purchase_id=PURCHASE_ID,
        buff_order_id=ORDER_ID,
        account_id=ACCOUNT_ID,
        recipient_steam_id=RECIPIENT_STEAM_ID,
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=status,
        steam_tradeoffer_id=None,
        offer_attempted_at=(10.0 if status is DeliveryStatus.OFFER_ATTEMPTED else None),
        offer_sent_at=None,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id=None,
    )
    validate_delivery_snapshot(snapshot)
    return StoredDelivery(snapshot, revision)


def _coordinator(
    item: StoredDelivery,
    *,
    read_factory,
    eligibility_factory=None,
    send_factory=None,
):
    store = RecordingStore(item)
    read_adapter = RecordingAdapter(
        PlatformCapability.READ_OFFER_STATE,
        read_factory,
    )
    eligibility_adapter = RecordingAdapter(
        PlatformCapability.READ_BUYER_SEND_ELIGIBILITY,
        eligibility_factory
        or (
            lambda request: PlatformResult(
                request,
                PlatformResultStatus.SUCCESS,
                evidence=DeliveryDirectionEvidence("buyer_sends_offer"),
            )
        ),
    )
    send_adapter = RecordingAdapter(
        PlatformCapability.SEND_OFFER,
        send_factory
        or (
            lambda request: PlatformResult(
                request,
                PlatformResultStatus.RESULT_UNKNOWN,
                "offer_created_unproven",
            )
        ),
    )
    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.READ_BUYER_SEND_ELIGIBILITY: eligibility_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=1.0,
        allow_writes=True,
        clock=lambda: 12.0,
    )
    return coordinator, store, read_adapter, eligibility_adapter, send_adapter


def _order_not_proven(request):
    return PlatformResult(
        request,
        PlatformResultStatus.RESULT_UNKNOWN,
        "order_not_proven",
    )


def test_attempted_read_miss_never_reopens_send_authority_and_logs_secret_free(caplog):
    item = _delivery(DeliveryStatus.OFFER_ATTEMPTED)
    coordinator, store, read_adapter, eligibility_adapter, send_adapter = _coordinator(
        item,
        read_factory=_order_not_proven,
    )

    with caplog.at_level(logging.INFO, logger="app.auto_offer.coordinator"):
        result = coordinator.step(item)

    assert result.after == item
    assert result.persisted is False
    assert result.decision.result is AutoOfferResult.WAITING
    assert len(read_adapter.calls) == 1
    assert coordinator.read_send_authority(item) is None
    assert eligibility_adapter.calls == []
    assert send_adapter.calls == []
    assert store.advance_calls == []

    messages = [
        record.getMessage()
        for record in caplog.records
        if "auto_offer_post_send_binding" in record.getMessage()
    ]
    assert messages == [
        "auto_offer_post_send_binding capability=read_offer_state "
        "status=result_unknown detail=order_not_proven persisted=false decision=waiting"
    ]
    for secret_like_value in (
        PURCHASE_ID,
        ORDER_ID,
        ACCOUNT_ID,
        RECIPIENT_STEAM_ID,
        COUNTERPARTY_STEAM_ID,
    ):
        assert secret_like_value not in messages[0]


def test_repeated_attempted_ticks_can_repeat_only_binding_read_never_send():
    item = _delivery(DeliveryStatus.OFFER_ATTEMPTED)
    coordinator, store, read_adapter, eligibility_adapter, send_adapter = _coordinator(
        item,
        read_factory=_order_not_proven,
    )

    for _ in range(3):
        result = coordinator.step(item)
        assert result.after == item
        assert result.persisted is False
        assert result.decision.result is AutoOfferResult.WAITING
        assert coordinator.read_send_authority(item) is None

    assert len(read_adapter.calls) == 3
    assert eligibility_adapter.calls == []
    assert send_adapter.calls == []
    assert store.advance_calls == []


def test_attempted_exact_binding_persists_offer_sent_without_send_and_logs(caplog):
    item = _delivery(DeliveryStatus.OFFER_ATTEMPTED)

    def exact_offer(request):
        return PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=OfferStateEvidence(
                "task101-offer-exact",
                COUNTERPARTY_STEAM_ID,
            ),
        )

    coordinator, store, read_adapter, eligibility_adapter, send_adapter = _coordinator(
        item,
        read_factory=exact_offer,
    )

    with caplog.at_level(logging.INFO, logger="app.auto_offer.coordinator"):
        result = coordinator.step(item)

    assert result.persisted is True
    assert result.after == store.current
    assert result.after.revision == item.revision + 1
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert result.after.snapshot.steam_tradeoffer_id == "task101-offer-exact"
    assert result.after.snapshot.counterparty_steam_id == COUNTERPARTY_STEAM_ID
    assert result.after.snapshot.offer_sent_at == 12.0
    assert len(read_adapter.calls) == 1
    assert eligibility_adapter.calls == []
    assert send_adapter.calls == []

    messages = [
        record.getMessage()
        for record in caplog.records
        if "auto_offer_post_send_binding" in record.getMessage()
    ]
    assert messages == [
        "auto_offer_post_send_binding capability=read_offer_state "
        "status=success detail=offer_bound persisted=true decision=waiting"
    ]
    assert "task101-offer-exact" not in messages[0]
    assert COUNTERPARTY_STEAM_ID not in messages[0]


def test_awaiting_offer_keeps_exactly_one_first_send_then_attempted_is_read_only():
    item = _delivery(DeliveryStatus.AWAITING_OFFER, revision=3)
    coordinator, store, _read_adapter, eligibility_adapter, send_adapter = _coordinator(
        item,
        read_factory=_order_not_proven,
    )

    proof = coordinator.read_send_authority(item)
    assert proof is not None
    assert len(eligibility_adapter.calls) == 1

    send_result = coordinator.send_offer_with_authority(item, proof)
    attempted = send_result.after
    assert attempted == store.current
    assert attempted.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED
    assert attempted.revision == item.revision + 1
    assert len(send_adapter.calls) == 1

    assert coordinator.read_send_authority(attempted) is None
    assert len(eligibility_adapter.calls) == 1
    assert len(send_adapter.calls) == 1


def test_attempted_state_cannot_consume_even_an_unconsumed_first_send_proof():
    item = _delivery(DeliveryStatus.AWAITING_OFFER, revision=7)
    coordinator, store, _read_adapter, eligibility_adapter, send_adapter = _coordinator(
        item,
        read_factory=_order_not_proven,
    )
    proof = coordinator.read_send_authority(item)
    assert proof is not None
    assert len(eligibility_adapter.calls) == 1

    forged_attempted = StoredDelivery(
        replace(
            item.snapshot,
            delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
            offer_attempted_at=10.0,
        ),
        item.revision,
    )
    validate_delivery_snapshot(forged_attempted.snapshot)
    store.current = forged_attempted

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="send_authority_not_available",
    ):
        coordinator.send_offer_with_authority(forged_attempted, proof)

    assert send_adapter.calls == []
