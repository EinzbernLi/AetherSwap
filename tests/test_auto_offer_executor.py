from dataclasses import FrozenInstanceError, replace

import pytest

from app.auto_offer.contracts import AutoOfferResult, DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.executor import (
    DeliveryExecutor,
    DeliveryExecutorError,
    DeliveryResult,
    MockDeliveryExecutor,
    retry_is_allowed,
)
from app.auto_offer.store import StoredDelivery


def snapshot(**changes):
    value = DeliverySnapshot(
        purchase_id="purchase-1",
        buff_order_id="buff-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
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
    return replace(value, **changes)


def stored(**changes):
    revision = changes.pop("revision", 1)
    return StoredDelivery(snapshot=snapshot(**changes), revision=revision)


def test_executor_is_abstract_and_mock_accepts_stored_delivery():
    with pytest.raises(TypeError):
        DeliveryExecutor()

    delivery = stored()
    result = MockDeliveryExecutor().execute(delivery)
    assert result.delivery is delivery
    assert result.result is AutoOfferResult.WAITING
    assert result.blocks_next_purchase is True


def test_completed_delivery_returns_nonblocking_complete_result():
    delivery = stored(
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.RECEIVED,
        steam_tradeoffer_id="offer-1",
        offer_attempted_at=1.0,
        offer_sent_at=2.0,
        received_at=3.0,
        pending_receipt=False,
        assetid="asset-1",
    )
    result = MockDeliveryExecutor().execute(delivery)
    assert result.result is AutoOfferResult.COMPLETE
    assert result.blocks_next_purchase is False
    assert result.retryable is False


def test_unknown_state_fails_closed_without_retry():
    delivery = stored(
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    result = MockDeliveryExecutor().execute(delivery)
    assert result.result is AutoOfferResult.RESULT_UNKNOWN
    assert result.blocks_next_purchase is True
    assert result.retryable is False


def test_repeat_execution_for_the_same_revision_is_idempotent():
    executor = MockDeliveryExecutor()
    delivery = stored()
    first = executor.execute(delivery)
    second = executor.execute(delivery)
    assert second is first


def test_failure_result_does_not_mutate_store_state():
    delivery = stored(delivery_status=DeliveryStatus.BLOCKED)
    before = delivery
    result = MockDeliveryExecutor().execute(delivery)
    assert result.result is AutoOfferResult.BLOCKED
    assert result.retryable is False
    assert delivery == before
    assert result.delivery == before


def test_retry_requires_waiting_result_and_a_new_persisted_revision():
    executor = MockDeliveryExecutor()
    original = stored()
    waiting = executor.execute(original)
    assert retry_is_allowed(waiting, original) is False
    assert retry_is_allowed(waiting, stored(revision=2)) is True

    unknown = executor.execute(
        stored(
            revision=2,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            delivery_error="write_result_unknown",
        )
    )
    assert retry_is_allowed(unknown, stored(revision=3)) is False


def test_same_purchase_and_revision_with_different_snapshot_fails_closed():
    executor = MockDeliveryExecutor()
    executor.execute(stored())
    with pytest.raises(DeliveryExecutorError):
        executor.execute(stored(delivery_error="contract_unknown"))


def test_invalid_or_unknown_delivery_fails_closed():
    executor = MockDeliveryExecutor()
    with pytest.raises(DeliveryExecutorError):
        executor.execute(object())

    unknown_status = StoredDelivery(
        snapshot=replace(snapshot(), delivery_status="unknown-status"),
        revision=1,
    )
    with pytest.raises(DeliveryExecutorError):
        executor.execute(unknown_status)


def test_result_is_immutable_and_rejects_unsafe_retry_contract():
    result = MockDeliveryExecutor().execute(stored())
    with pytest.raises(FrozenInstanceError):
        result.retryable = False
    with pytest.raises(DeliveryExecutorError):
        DeliveryResult(stored(), AutoOfferResult.BLOCKED, True)
