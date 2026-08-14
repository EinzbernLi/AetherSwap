import copy
import pickle
from dataclasses import FrozenInstanceError, replace
from math import inf, nan

import pytest

from app.auto_offer.adapters import (
    CompletedTradeItemEvidence,
    ConfirmOfferEvidence,
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformAdapterError,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformAdapterUnsupportedError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    RecipientInventoryItemEvidence,
    SellerOrderItemEvidence,
    SteamCompletedTradeEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from app.auto_offer.coordinator import (
    ReadOnlyCoordinatorBlockedError,
    ReadOnlyCoordinatorConflictError,
    ReadOnlyCoordinatorError,
    ReadOnlyDeliveryCoordinator,
    ReadOnlyStepResult,
)
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.store import (
    AutoOfferStoreConflictError,
    AutoOfferStoreError,
    AutoOfferStoreStaleWriteError,
    StoredDelivery,
)


IDENTITY = {
    "purchase_id": "purchase-1",
    "buff_order_id": "buff-order-1",
    "account_id": "account-1",
    "recipient_steam_id": "steam-1",
}
_UNSET = object()


def make_snapshot(
    status=DeliveryStatus.PENDING_DIRECTION,
    mode=None,
    *,
    steam_tradeoffer_id=None,
    offer_attempted_at=None,
    offer_sent_at=None,
    pending_receipt=True,
    received_at=None,
    assetid=None,
    delivery_error=None,
    counterparty_steam_id=_UNSET,
):
    if counterparty_steam_id is _UNSET:
        counterparty_steam_id = (
            "counterparty-1"
            if mode is DeliveryMode.SELLER_SENDS_OFFER
            and status is not DeliveryStatus.PENDING_DIRECTION
            else None
        )
    value = DeliverySnapshot(
        **IDENTITY,
        delivery_mode=mode,
        delivery_status=status,
        steam_tradeoffer_id=steam_tradeoffer_id,
        offer_attempted_at=offer_attempted_at,
        offer_sent_at=offer_sent_at,
        received_at=received_at,
        delivery_error=delivery_error,
        pending_receipt=pending_receipt,
        assetid=assetid,
        counterparty_steam_id=counterparty_steam_id,
    )
    validate_delivery_snapshot(value)
    return value


def make_delivery(snapshot=None, revision=1):
    return StoredDelivery(snapshot=snapshot or make_snapshot(), revision=revision)


class SpyStore:
    def __init__(self, current=None, *, get_error=None, advance_error=None):
        self.current = current
        self.get_error = get_error
        self.advance_error = advance_error
        self.get_calls = []
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id):
        self.get_calls.append(purchase_id)
        if self.get_error is not None:
            raise self.get_error
        return self.current

    def advance(self, current, target):
        self.advance_calls.append((current, target))
        if self.advance_error is not None:
            raise self.advance_error
        return StoredDelivery(snapshot=target, revision=current.revision + 1)


class SpyAdapter:
    def __init__(self, capabilities, result_factory=None, error=None):
        self.capabilities = frozenset(capabilities)
        self.result_factory = result_factory
        self.error = error
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.result_factory is None:
            return None
        return self.result_factory(request)


def success_factory(evidence):
    return lambda request: PlatformResult(
        request=request,
        status=PlatformResultStatus.SUCCESS,
        evidence=evidence,
    )


def steam_offer_evidence(*, is_our_offer, lifecycle):
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id="offer-1",
        account_steam_id="steam-1",
        counterparty_steam_id="counterparty-1",
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=(),
        items_to_receive=(TradeOfferItemEvidence(730, "2", "offer-asset-1", 1),),
    )


def completed_trade_evidence(
    *,
    steam_tradeoffer_id="offer-1",
    steam_trade_id="trade-1",
    completed_at=3.0,
    items_given=(),
    items_received=None,
    inventory_confirmed_items=None,
):
    received = items_received
    if received is None:
        received = (
            CompletedTradeItemEvidence(
                730,
                "2",
                "source-asset-1",
                1,
                "3",
                "new-asset-1",
            ),
        )
    confirmed = inventory_confirmed_items
    if confirmed is None:
        item = received[0]
        confirmed = (
            RecipientInventoryItemEvidence(
                item.appid,
                item.new_contextid,
                item.new_assetid,
                item.amount,
            ),
        )
    return SteamCompletedTradeEvidence(
        steam_tradeoffer_id=steam_tradeoffer_id,
        steam_trade_id=steam_trade_id,
        account_steam_id="steam-1",
        counterparty_steam_id="counterparty-1",
        completed_at=completed_at,
        items_given=items_given,
        items_received=received,
        inventory_confirmed_items=confirmed,
    )


def coordinator_for(item, adapter=None, *, store=None, timeout=7.5, capability=None):
    store = store or SpyStore(item)
    if adapter is None:
        capability = capability or PlatformCapability.READ_DELIVERY_DIRECTION
        adapter = SpyAdapter(
            {capability},
            success_factory(
                DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1")
            ),
        )
    capability = capability or next(iter(adapter.capabilities))
    return (
        ReadOnlyDeliveryCoordinator(
            store,
            {capability: adapter},
            timeout_seconds=timeout,
        ),
        store,
        adapter,
    )


def test_result_is_frozen_and_invalid_direct_construction_is_rejected():
    item = make_delivery()
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_DELIVERY_DIRECTION},
        success_factory(
            DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1")
        ),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=5.0,
    ).step(item)
    assert isinstance(result, ReadOnlyStepResult)
    with pytest.raises(FrozenInstanceError):
        result.persisted = True
    with pytest.raises(ReadOnlyCoordinatorError):
        ReadOnlyStepResult(object(), object(), object(), object(), False)


def test_constructor_rejects_invalid_store_and_non_mapping_registry():
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_store"):
        ReadOnlyDeliveryCoordinator(
            object(), {}, timeout_seconds=1.0
        )
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_adapter_registry"):
        ReadOnlyDeliveryCoordinator(
            SpyStore(make_delivery()), [], timeout_seconds=1.0
        )


def test_constructor_rejects_send_offer_and_capability_mismatch():
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="write_capability_not_allowed"):
        ReadOnlyDeliveryCoordinator(
            SpyStore(make_delivery()),
            {PlatformCapability.SEND_OFFER: SpyAdapter({PlatformCapability.SEND_OFFER})},
            timeout_seconds=1.0,
        )


def test_constructor_accepts_read_steam_trade_offer_adapter():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        success_factory(
            steam_offer_evidence(
                is_our_offer=False,
                lifecycle=SteamTradeOfferLifecycle.ACTIVE,
            )
        ),
    )
    result = ReadOnlyDeliveryCoordinator(
        SpyStore(item),
        {PlatformCapability.READ_STEAM_TRADE_OFFER: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.request.capability is PlatformCapability.READ_STEAM_TRADE_OFFER
    with pytest.raises(ReadOnlyCoordinatorError, match="adapter_capability_mismatch"):
        ReadOnlyDeliveryCoordinator(
            SpyStore(make_delivery()),
            {
                PlatformCapability.READ_DELIVERY_DIRECTION: SpyAdapter(
                    {PlatformCapability.READ_OFFER_STATE}
                )
            },
            timeout_seconds=1.0,
        )


def test_constructor_accepts_completed_trade_adapter():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        success_factory(completed_trade_evidence()),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        SpyStore(item),
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=1.0,
    )
    assert isinstance(coordinator, ReadOnlyDeliveryCoordinator)


@pytest.mark.parametrize("timeout", [True, 0, -1, inf, nan])
def test_constructor_rejects_invalid_timeout(timeout):
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_timeout"):
        ReadOnlyDeliveryCoordinator(
            SpyStore(make_delivery()), {}, timeout_seconds=timeout
        )


def test_invalid_supplied_delivery_is_rejected_before_store_or_adapter():
    item = make_delivery(revision=True)
    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_delivery"):
        coordinator.step(item)
    assert store.get_calls == []
    assert adapter.calls == []


@pytest.mark.parametrize(
    "persisted",
    [
        None,
        make_delivery(revision=2),
    ],
)
def test_persisted_current_conflicts_happen_before_adapter(persisted):
    item = make_delivery()
    if persisted is None:
        persisted = None
    store = SpyStore(persisted)
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorConflictError, match="persisted_delivery_mismatch"):
        coordinator.step(item)
    assert len(store.get_calls) == 1
    assert store.advance_calls == []
    assert adapter.calls == []


def test_snapshot_mismatch_conflicts_before_adapter():
    item = make_delivery()
    persisted_snapshot = DeliverySnapshot(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
        delivery_mode=None,
        delivery_status=DeliveryStatus.PENDING_DIRECTION,
        steam_tradeoffer_id=None,
        offer_attempted_at=None,
        offer_sent_at=None,
        received_at=None,
        delivery_error="module_contract_mismatch",
        pending_receipt=True,
        assetid=None,
    )
    store = SpyStore(make_delivery(persisted_snapshot))
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorConflictError):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.advance_calls == []


@pytest.mark.parametrize(
    "error,expected",
    [
        (AutoOfferStoreStaleWriteError("secret"), ReadOnlyCoordinatorConflictError),
        (AutoOfferStoreConflictError("secret"), ReadOnlyCoordinatorConflictError),
        (AutoOfferStoreError("secret"), ReadOnlyCoordinatorError),
        (RuntimeError("sqlite token"), ReadOnlyCoordinatorError),
    ],
)
def test_store_preflight_failures_are_stable_and_not_leaked(error, expected):
    item = make_delivery()
    store = SpyStore(item, get_error=error)
    coordinator, _, adapter = coordinator_for(item, store=store)
    with pytest.raises(expected) as exc_info:
        coordinator.step(item)
    assert "secret" not in str(exc_info.value)
    assert "sqlite" not in str(exc_info.value)
    assert adapter.calls == []


def test_direction_routes_exact_request_and_advances_once():
    item = make_delivery()
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_DELIVERY_DIRECTION},
        success_factory(
            DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1")
        ),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=9.25,
    )
    result = coordinator.step(item)
    request = adapter.calls[0]
    assert request.purchase_id == item.snapshot.purchase_id
    assert request.buff_order_id == item.snapshot.buff_order_id
    assert request.account_id == item.snapshot.account_id
    assert request.recipient_steam_id == item.snapshot.recipient_steam_id
    assert request.revision == item.revision
    assert request.capability is PlatformCapability.READ_DELIVERY_DIRECTION
    assert request.timeout_seconds == 9.25
    assert request.steam_tradeoffer_id is None
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1
    assert result.persisted is True
    assert result.after.revision == item.revision + 1
    assert result.after.snapshot == result.decision.target
    assert result.decision.target.delivery_status is DeliveryStatus.AWAITING_OFFER
    assert result.decision.target.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER


def test_seller_offer_routes_and_persists_exact_tradeoffer_id_once():
    item = make_delivery(
        make_snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.SELLER_SENDS_OFFER)
    )
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_OFFER_STATE},
        success_factory(OfferStateEvidence("offer-42", "76561198000000002")),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: adapter},
        timeout_seconds=4.0,
    )
    result = coordinator.step(item)
    assert len(adapter.calls) == 1
    assert adapter.calls[0].capability is PlatformCapability.READ_OFFER_STATE
    assert adapter.calls[0].steam_tradeoffer_id is None
    assert len(store.advance_calls) == 1
    assert result.after.snapshot.steam_tradeoffer_id == "offer-42"
    assert result.after.snapshot == result.decision.target
    assert result.after.revision == 2


def test_awaiting_inventory_routes_completed_trade_without_inventory_fallback():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(item)
    inventory_adapter = SpyAdapter(
        {PlatformCapability.READ_INVENTORY_STATE},
        success_factory(InventoryStateEvidence(("asset-1",), 1)),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_INVENTORY_STATE: inventory_adapter},
        timeout_seconds=2.0,
    ).step(item)
    assert inventory_adapter.calls == []
    assert result.platform_result.request.capability is (
        PlatformCapability.READ_STEAM_COMPLETED_TRADE
    )
    assert result.platform_result.request.steam_tradeoffer_id == "offer-1"
    assert result.platform_result.status is PlatformResultStatus.UNSUPPORTED
    assert result.platform_result.detail == "adapter_not_available"
    assert result.persisted is False
    assert result.after == item
    assert result.decision.target is None
    assert result.decision.detail == "unsupported_capability"
    assert store.advance_calls == []
    assert item.snapshot.assetid is None
    assert item.snapshot.received_at is None
    assert item.snapshot.pending_receipt is True


@pytest.mark.parametrize(
    "status",
    [PlatformResultStatus.RESULT_UNKNOWN, PlatformResultStatus.TIMEOUT],
)
def test_completed_trade_non_success_never_persists(status):
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        lambda request: PlatformResult(request, status, "read_pending"),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert len(adapter.calls) == 1
    assert adapter.calls[0].steam_tradeoffer_id == "offer-1"
    assert result.persisted is False
    assert result.after == item
    assert result.decision.target is None
    assert store.advance_calls == []


@pytest.mark.parametrize(
    "evidence,expected_result,expected_detail",
    [
        (
            completed_trade_evidence(
                items_given=(
                    CompletedTradeItemEvidence(
                        730, "2", "given-source", 1, "3", "given-new"
                    ),
                )
            ),
            AutoOfferResult.BLOCKED,
            "completed_trade_outgoing_items_present",
        ),
        (
            completed_trade_evidence(
                items_received=(
                    CompletedTradeItemEvidence(
                        730, "2", "source-1", 1, "3", "new-1"
                    ),
                    CompletedTradeItemEvidence(
                        730, "2", "source-2", 1, "3", "new-2"
                    ),
                ),
                inventory_confirmed_items=(),
            ),
            AutoOfferResult.BLOCKED,
            "purchase_asset_attribution_ambiguous",
        ),
        (
            completed_trade_evidence(inventory_confirmed_items=()),
            AutoOfferResult.WAITING,
            "recipient_inventory_not_confirmed",
        ),
    ],
)
def test_completed_trade_unsafe_or_unconfirmed_evidence_never_advances(
    evidence, expected_result, expected_detail
):
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        success_factory(evidence),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert len(adapter.calls) == 1
    assert result.decision.result is expected_result
    assert result.decision.detail == expected_detail
    assert result.decision.target is None
    assert result.persisted is False
    assert store.advance_calls == []


def test_completed_trade_confirmed_single_item_persists_received_once():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        ),
        revision=4,
    )
    store = SpyStore(item)
    evidence = completed_trade_evidence(completed_at=3.0)
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        success_factory(evidence),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=6.0,
    ).step(item)
    request = adapter.calls[0]
    assert request.capability is PlatformCapability.READ_STEAM_COMPLETED_TRADE
    assert request.steam_tradeoffer_id == "offer-1"
    assert request.revision == 4
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1
    assert result.persisted is True
    assert result.decision.result is AutoOfferResult.COMPLETE
    assert result.after.snapshot.delivery_status is DeliveryStatus.RECEIVED
    assert result.after.snapshot.assetid == "new-asset-1"
    assert result.after.snapshot.received_at == 3.0
    assert result.after.snapshot.pending_receipt is False
    assert result.after.snapshot.steam_tradeoffer_id == "offer-1"
    assert result.after.revision == item.revision + 1


def test_completed_trade_forged_returned_offer_id_is_malformed_without_advance():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )

    def forged_result(request):
        forged_request = PlatformRequest(
            purchase_id=request.purchase_id,
            buff_order_id=request.buff_order_id,
            account_id=request.account_id,
            recipient_steam_id=request.recipient_steam_id,
            revision=request.revision,
            capability=request.capability,
            timeout_seconds=request.timeout_seconds,
            steam_tradeoffer_id="offer-2",
        )
        return PlatformResult(
            forged_request,
            PlatformResultStatus.SUCCESS,
            evidence=completed_trade_evidence(steam_tradeoffer_id="offer-2"),
        )

    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE}, forged_result
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is PlatformResultStatus.MALFORMED
    assert result.platform_result.detail == "adapter_result_invalid"
    assert result.platform_result.evidence is None
    assert result.persisted is False
    assert store.advance_calls == []


def test_completed_trade_receipt_stale_write_has_no_retry():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(
        item, advance_error=AutoOfferStoreStaleWriteError("secret")
    )
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        success_factory(completed_trade_evidence()),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_COMPLETED_TRADE: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorConflictError, match="stale_write"):
        coordinator.step(item)
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1


def test_buyer_path_blocks_before_adapter_and_advance():
    item = make_delivery(make_snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.BUYER_SENDS_OFFER))
    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_OFFER_STATE})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="normal_send_authority_required"):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.advance_calls == []


@pytest.mark.parametrize(
    "status,mode,kwargs",
    [
        (DeliveryStatus.OFFER_ATTEMPTED, DeliveryMode.BUYER_SENDS_OFFER, {"offer_attempted_at": 1.0}),
        (DeliveryStatus.RESULT_UNKNOWN, DeliveryMode.BUYER_SENDS_OFFER, {"delivery_error": "write_result_unknown"}),
        (DeliveryStatus.RECEIVED, DeliveryMode.SELLER_SENDS_OFFER, {"steam_tradeoffer_id": "offer-1", "received_at": 3.0, "pending_receipt": False, "assetid": "asset-1"}),
        (DeliveryStatus.BLOCKED, None, {}),
        (DeliveryStatus.CANCELLED, None, {}),
        (DeliveryStatus.REFUNDED, None, {}),
    ],
)
def test_non_read_ready_states_never_call_adapter_or_advance(status, mode, kwargs):
    item = make_delivery(make_snapshot(status, mode, **kwargs))
    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="read_step_not_available"):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.advance_calls == []


@pytest.mark.parametrize(
    "status,mode,kwargs,is_our_offer,lifecycle,expected_status,advance_count",
    [
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACTIVE,
            DeliveryStatus.OFFER_CONFIRMED,
            1,
        ),
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACCEPTED,
            DeliveryStatus.OFFER_CONFIRMED,
            1,
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACTIVE,
            DeliveryStatus.OFFER_CONFIRMED,
            1,
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACCEPTED,
            DeliveryStatus.OFFER_CONFIRMED,
            1,
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACTIVE,
            None,
            0,
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACCEPTED,
            DeliveryStatus.AWAITING_INVENTORY,
            1,
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACTIVE,
            None,
            0,
        ),
        (
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACCEPTED,
            DeliveryStatus.AWAITING_INVENTORY,
            1,
        ),
    ],
)
def test_trade_offer_routes_copy_exact_id_and_advance_once(
    status, mode, kwargs, is_our_offer, lifecycle, expected_status, advance_count
):
    item = make_delivery(
        make_snapshot(status, mode, steam_tradeoffer_id="offer-1", **kwargs)
    )
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        success_factory(steam_offer_evidence(is_our_offer=is_our_offer, lifecycle=lifecycle)),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_TRADE_OFFER: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert len(store.get_calls) == 1
    assert len(adapter.calls) == 1
    assert adapter.calls[0].capability is PlatformCapability.READ_STEAM_TRADE_OFFER
    assert adapter.calls[0].steam_tradeoffer_id == "offer-1"
    assert len(store.advance_calls) == advance_count
    assert result.persisted is (advance_count == 1)
    if expected_status is None:
        assert result.after == item
        assert result.decision.target is None
        assert result.decision.detail == "trade_offer_not_accepted"
    else:
        assert result.after.snapshot.delivery_status is expected_status
        assert result.after.snapshot.assetid is None
        assert result.after.snapshot.received_at is None
        assert result.after.snapshot.pending_receipt is True


def test_trade_offer_missing_adapter_is_unsupported_without_advance():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(item)
    result = ReadOnlyDeliveryCoordinator(store, {}, timeout_seconds=1.0).step(item)
    assert result.platform_result.status is PlatformResultStatus.UNSUPPORTED
    assert result.platform_result.detail == "adapter_not_available"
    assert result.persisted is False
    assert store.advance_calls == []


def test_trade_offer_forged_request_id_is_normalized_to_malformed():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )

    def forged_result(request):
        forged_request = PlatformRequest(
            purchase_id=request.purchase_id,
            buff_order_id=request.buff_order_id,
            account_id=request.account_id,
            recipient_steam_id=request.recipient_steam_id,
            revision=request.revision,
            capability=request.capability,
            timeout_seconds=request.timeout_seconds,
            steam_tradeoffer_id="offer-2",
        )
        return PlatformResult(
            forged_request,
            PlatformResultStatus.SUCCESS,
            evidence=SteamTradeOfferEvidence(
                steam_tradeoffer_id="offer-2",
                account_steam_id="steam-1",
                counterparty_steam_id="counterparty-1",
                is_our_offer=False,
                lifecycle=SteamTradeOfferLifecycle.ACTIVE,
                items_to_give=(),
                items_to_receive=(TradeOfferItemEvidence(730, "2", "offer-asset-1", 1),),
            ),
        )

    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_STEAM_TRADE_OFFER}, forged_result)
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_TRADE_OFFER: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is PlatformResultStatus.MALFORMED
    assert result.platform_result.detail == "adapter_result_invalid"
    assert result.platform_result.evidence is None
    assert result.persisted is False
    assert store.advance_calls == []


def test_missing_adapter_is_normalized_and_planned_without_advance():
    item = make_delivery()
    store = SpyStore(item)
    result = ReadOnlyDeliveryCoordinator(store, {}, timeout_seconds=1.0).step(item)
    assert result.platform_result.status is PlatformResultStatus.UNSUPPORTED
    assert result.platform_result.detail == "adapter_not_available"
    assert result.decision.target is None
    assert result.persisted is False
    assert result.after == item
    assert store.advance_calls == []


@pytest.mark.parametrize("raw", [None, object(), "success"])
def test_invalid_adapter_result_is_malformed(raw):
    item = make_delivery()
    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION}, lambda _: raw)
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is PlatformResultStatus.MALFORMED
    assert result.platform_result.detail == "adapter_result_invalid"
    assert result.platform_result.request == adapter.calls[0]
    assert result.platform_result.evidence is None
    assert result.persisted is False
    assert store.advance_calls == []


def test_wrong_adapter_request_is_not_trusted():
    item = make_delivery()
    wrong = PlatformRequest(
        purchase_id="other-purchase",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
        revision=1,
        capability=PlatformCapability.READ_DELIVERY_DIRECTION,
        timeout_seconds=1.0,
    )
    adapter = SpyAdapter(
        {PlatformCapability.READ_DELIVERY_DIRECTION},
        lambda _: PlatformResult(wrong, PlatformResultStatus.RESULT_UNKNOWN),
    )
    store = SpyStore(item)
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is PlatformResultStatus.MALFORMED
    assert result.platform_result.request == adapter.calls[0]
    assert result.decision.target is None


def test_forged_success_evidence_is_not_trusted():
    item = make_delivery()
    forged = object.__new__(PlatformResult)
    object.__setattr__(forged, "request", None)
    object.__setattr__(forged, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged, "detail", "forged")
    object.__setattr__(
        forged,
        "evidence",
        DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1"),
    )
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION}, lambda _: forged)
    result = ReadOnlyDeliveryCoordinator(
        SpyStore(item),
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is PlatformResultStatus.MALFORMED
    assert result.platform_result.detail == "adapter_result_invalid"
    assert result.platform_result.evidence is None


@pytest.mark.parametrize(
    "error,expected_status,expected_detail",
    [
        (PlatformAdapterTimeoutError("secret timeout"), PlatformResultStatus.TIMEOUT, "adapter_timeout"),
        (PlatformAdapterUnsupportedError("secret unsupported"), PlatformResultStatus.UNSUPPORTED, "adapter_unsupported"),
        (PlatformAdapterProtocolError("secret protocol"), PlatformResultStatus.MALFORMED, "adapter_protocol_error"),
        (PlatformAdapterError("secret adapter"), PlatformResultStatus.FAILURE, "adapter_failure"),
        (RuntimeError("secret token"), PlatformResultStatus.FAILURE, "adapter_internal_error"),
    ],
)
def test_adapter_exceptions_are_normalized_without_raw_details(error, expected_status, expected_detail):
    item = make_delivery()
    adapter = SpyAdapter({PlatformCapability.READ_DELIVERY_DIRECTION}, error=error)
    result = ReadOnlyDeliveryCoordinator(
        SpyStore(item),
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    ).step(item)
    assert result.platform_result.status is expected_status
    assert result.platform_result.detail == expected_detail
    assert "secret" not in result.platform_result.detail
    assert result.platform_result.evidence is None
    assert result.persisted is False


def test_stale_write_is_conflict_without_retrying_adapter_or_advance():
    item = make_delivery()
    store = SpyStore(item, advance_error=AutoOfferStoreStaleWriteError("secret"))
    adapter = SpyAdapter(
        {PlatformCapability.READ_DELIVERY_DIRECTION},
        success_factory(
            DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1")
        ),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorConflictError, match="stale_write"):
        coordinator.step(item)
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        (AutoOfferStoreConflictError("secret"), ReadOnlyCoordinatorConflictError),
        (AutoOfferStoreError("secret"), ReadOnlyCoordinatorError),
        (RuntimeError("secret"), ReadOnlyCoordinatorError),
    ],
)
def test_other_advance_failures_are_stable(error, expected):
    item = make_delivery()
    store = SpyStore(item, advance_error=error)
    adapter = SpyAdapter(
        {PlatformCapability.READ_DELIVERY_DIRECTION},
        success_factory(
            DeliveryDirectionEvidence(counterparty_steam_id="counterparty-1")
        ),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_DELIVERY_DIRECTION: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(expected) as exc_info:
        coordinator.step(item)
    assert "secret" not in str(exc_info.value)
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1


def test_coordinator_has_no_lifecycle_sqlite_or_runtime_surface():
    path = __import__("pathlib").Path(__file__).parents[1] / "app" / "auto_offer" / "coordinator.py"
    text = path.read_text(encoding="utf-8")
    forbidden = (
        "import sqlite3",
        "initialize(",
        "close(",
        "ensure_initial(",
        "Pipeline",
        "Purchase Flow",
        "requests",
        "httpx",
        "aiohttp",
        "sleep(",
        "Thread",
        "run_forever",
        "execute_all",
    )
    for term in forbidden:
        assert term not in text


class RecordingStore:
    def __init__(self, current, *, fail_on_advance=None, failure=None, events=None):
        self.current = current
        self.fail_on_advance = fail_on_advance
        self.failure = failure or AutoOfferStoreError("secret-store-error")
        self.events = [] if events is None else events
        self.advance_count = 0

    def get_by_purchase_id(self, purchase_id):
        self.events.append(("get", purchase_id, self.current.revision))
        return self.current

    def advance(self, current, target):
        self.advance_count += 1
        self.events.append(
            (
                "advance",
                current.snapshot.delivery_status,
                target.delivery_status,
                current.revision,
            )
        )
        if self.fail_on_advance == self.advance_count:
            raise self.failure
        if current != self.current:
            raise AutoOfferStoreStaleWriteError("stale")
        self.current = StoredDelivery(target, current.revision + 1)
        return self.current


class RecordingSendAdapter:
    def __init__(self, result_factory=None, *, error=None, events=None, capabilities=None):
        self.capabilities = frozenset(
            {PlatformCapability.SEND_OFFER}
            if capabilities is None
            else capabilities
        )
        self.result_factory = result_factory
        self.error = error
        self.events = [] if events is None else events
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        self.events.append(("adapter", request.revision, request.buff_order_id))
        if self.error is not None:
            raise self.error
        if self.result_factory is None:
            return None
        return self.result_factory(request)


class RecordingDirectionAdapter:
    capabilities = frozenset({PlatformCapability.READ_DELIVERY_DIRECTION})

    def __init__(self, *, events=None, result_factory=None):
        self.events = [] if events is None else events
        self.calls = []
        self.result_factory = result_factory or success_factory(
            DeliveryDirectionEvidence("buyer_sends_offer")
        )

    def execute(self, request):
        self.calls.append(request)
        self.events.append(("authority_read", request.revision, request.buff_order_id))
        return self.result_factory(request)


def buyer_awaiting(revision=1):
    snapshot = make_snapshot(
        DeliveryStatus.AWAITING_OFFER,
        DeliveryMode.BUYER_SENDS_OFFER,
    )
    snapshot = replace(snapshot, recipient_steam_id="76561198000000001")
    return make_delivery(
        snapshot,
        revision=revision,
    )


def normal_send_coordinator(
    store,
    send_adapter=None,
    *,
    direction_adapter=None,
    timeout=1.0,
    clock=None,
):
    from app.auto_offer.coordinator import DeliveryCoordinator

    direction_adapter = direction_adapter or RecordingDirectionAdapter()
    adapters = {
        PlatformCapability.READ_DELIVERY_DIRECTION: direction_adapter,
    }
    if send_adapter is not None:
        adapters[PlatformCapability.SEND_OFFER] = send_adapter
    return DeliveryCoordinator(
        store,
        adapters,
        timeout_seconds=timeout,
        allow_writes=True,
        clock=clock or sequence_clock(10.0),
    ), direction_adapter


CONFIRM_COUNTERPARTY = "76561198000000002"


def buyer_confirmation_required(revision=5, *, counterparty=CONFIRM_COUNTERPARTY):
    snapshot = make_snapshot(
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryMode.BUYER_SENDS_OFFER,
        steam_tradeoffer_id="offer-1",
        offer_attempted_at=10.0,
        offer_sent_at=11.0,
        counterparty_steam_id=counterparty,
    )
    return make_delivery(
        replace(snapshot, recipient_steam_id="76561198000000001"),
        revision=revision,
    )


def exact_confirmation_steam_evidence(
    *,
    lifecycle=SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
    offer_id="offer-1",
    account_id="76561198000000001",
    counterparty=CONFIRM_COUNTERPARTY,
    is_our_offer=True,
    items_to_give=(),
):
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id=offer_id,
        account_steam_id=account_id,
        counterparty_steam_id=counterparty,
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=items_to_give,
        items_to_receive=(TradeOfferItemEvidence(730, "2", "offer-asset-1", 1),),
    )


class RecordingConfirmAdapter:
    capabilities = frozenset({PlatformCapability.CONFIRM_OFFER})

    def __init__(self, result_factory=None):
        self.result_factory = result_factory
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        if self.result_factory is None:
            return None
        return self.result_factory(request)


def normal_confirmation_coordinator(
    store,
    *,
    buff_evidence=None,
    steam_evidence=None,
    buff_result_factory=None,
    steam_result_factory=None,
    confirm_adapter=None,
):
    from app.auto_offer.coordinator import DeliveryCoordinator

    buff_evidence = buff_evidence or OfferStateEvidence(
        "offer-1",
        CONFIRM_COUNTERPARTY,
    )
    steam_evidence = steam_evidence or exact_confirmation_steam_evidence()
    buff_adapter = SpyAdapter(
        {PlatformCapability.READ_OFFER_STATE},
        buff_result_factory or success_factory(buff_evidence),
    )
    steam_adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        steam_result_factory or success_factory(steam_evidence),
    )
    adapters = {
        PlatformCapability.READ_OFFER_STATE: buff_adapter,
        PlatformCapability.READ_STEAM_TRADE_OFFER: steam_adapter,
    }
    if confirm_adapter is not None:
        adapters[PlatformCapability.CONFIRM_OFFER] = confirm_adapter
    coordinator = DeliveryCoordinator(
        store,
        adapters,
        timeout_seconds=1.0,
        allow_writes=True,
        allow_confirmation_writes=confirm_adapter is not None,
    )
    return coordinator, buff_adapter, steam_adapter


def buyer_result_unknown(revision=3):
    snapshot = make_snapshot(
        DeliveryStatus.RESULT_UNKNOWN,
        DeliveryMode.BUYER_SENDS_OFFER,
        offer_attempted_at=10.0,
        delivery_error="write_result_unknown",
    )
    return make_delivery(
        replace(snapshot, recipient_steam_id="76561198000000001"),
        revision=revision,
    )


def sequence_clock(*values):
    iterator = iter(values)
    return lambda: next(iterator)


def test_write_coordinator_is_same_authority_and_requires_explicit_enablement():
    from app.auto_offer.coordinator import DeliveryCoordinator

    assert ReadOnlyDeliveryCoordinator is DeliveryCoordinator
    item = buyer_awaiting()
    adapter = RecordingSendAdapter()
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="write_capability_not_allowed"):
        DeliveryCoordinator(
            RecordingStore(item),
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
        )
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_allow_writes"):
        DeliveryCoordinator(
            RecordingStore(item),
            {},
            timeout_seconds=1.0,
            allow_writes=1,
        )
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_clock"):
        DeliveryCoordinator(
            RecordingStore(item),
            {},
            timeout_seconds=1.0,
            allow_writes=True,
            clock=object(),
        )


def test_send_adapter_must_declare_only_send_offer():
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    adapter = RecordingSendAdapter(
        capabilities={
            PlatformCapability.SEND_OFFER,
            PlatformCapability.READ_OFFER_STATE,
        }
    )
    with pytest.raises(ReadOnlyCoordinatorError, match="adapter_capability_mismatch"):
        DeliveryCoordinator(
            RecordingStore(item),
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
            allow_writes=True,
        )


def test_normal_send_persists_attempt_before_single_call_and_result_unknown():
    from app.auto_offer.adapters import SendOfferEvidence
    from app.auto_offer.coordinator import DeliveryCoordinator, SendOfferStepResult

    events = []
    item = buyer_awaiting(revision=4)
    store = RecordingStore(item, events=events)
    adapter = RecordingSendAdapter(
        lambda request: PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=SendOfferEvidence("offer-42"),
        ),
        events=events,
    )
    coordinator, direction_adapter = normal_send_coordinator(
        store,
        adapter,
        direction_adapter=RecordingDirectionAdapter(events=events),
        timeout=7.0,
        clock=sequence_clock(10.0),
    )
    proof = coordinator.read_send_authority(item)
    result = coordinator.send_offer_with_authority(item, proof)

    assert isinstance(result, SendOfferStepResult)
    assert [event[0] for event in events] == [
        "get",
        "authority_read",
        "get",
        "advance",
        "adapter",
        "advance",
    ]
    assert events[3][1:3] == (
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
    )
    assert events[5][1:3] == (
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.RESULT_UNKNOWN,
    )
    assert len(direction_adapter.calls) == 1
    assert len(adapter.calls) == 1
    request = adapter.calls[0]
    assert request.revision == 5
    assert request.capability is PlatformCapability.SEND_OFFER
    assert request.steam_tradeoffer_id is None
    assert request.purchase_id == item.snapshot.purchase_id
    assert request.buff_order_id == item.snapshot.buff_order_id
    assert request.account_id == item.snapshot.account_id
    assert request.recipient_steam_id == item.snapshot.recipient_steam_id
    assert result.attempted.snapshot.offer_attempted_at == 10.0
    assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert result.after.snapshot.delivery_error == "write_result_unknown"
    assert result.after.snapshot.steam_tradeoffer_id is None
    assert result.after.snapshot.counterparty_steam_id is None
    assert result.after.snapshot.offer_sent_at is None
    assert result.after.revision == 6


def test_normal_send_proof_is_exact_opaque_single_use_and_process_local():
    item = buyer_awaiting(revision=7)
    store = RecordingStore(item)
    adapter = RecordingSendAdapter(
        lambda request: PlatformResult(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "offer_created_unproven",
        )
    )
    coordinator, direction_adapter = normal_send_coordinator(store, adapter)

    proof = coordinator.read_send_authority(item)
    assert repr(proof) == "<opaque normal send proof>"
    assert (
        proof.purchase_id,
        proof.buff_order_id,
        proof.account_id,
        proof.recipient_steam_id,
        proof.revision,
    ) == (
        item.snapshot.purchase_id,
        item.snapshot.buff_order_id,
        item.snapshot.account_id,
        item.snapshot.recipient_steam_id,
        item.revision,
    )
    with pytest.raises(TypeError, match="normal_send_proof_not_serializable"):
        pickle.dumps(proof)
    with pytest.raises(TypeError, match="normal_send_proof_not_serializable"):
        copy.copy(proof)

    result = coordinator.send_offer_with_authority(item, proof)
    assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert len(direction_adapter.calls) == 1
    assert len(adapter.calls) == 1

    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="send_authority_proof_required"):
        coordinator.send_offer_with_authority(item, proof)
    restarted, _ = normal_send_coordinator(store, adapter)
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="send_authority_proof_required"):
        restarted.send_offer_with_authority(item, proof)
    assert len(adapter.calls) == 1


def test_normal_send_proof_stale_store_revision_blocks_before_send():
    item = buyer_awaiting(revision=2)
    store = RecordingStore(item)
    adapter = RecordingSendAdapter()
    coordinator, _ = normal_send_coordinator(store, adapter)
    proof = coordinator.read_send_authority(item)
    store.current = StoredDelivery(item.snapshot, item.revision + 1)

    with pytest.raises(ReadOnlyCoordinatorConflictError, match="persisted_delivery_mismatch"):
        coordinator.send_offer_with_authority(item, proof)
    assert adapter.calls == []
    assert store.advance_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"purchase_id": "purchase-other"},
        {"buff_order_id": "buff-order-other"},
        {"account_id": "account-other"},
        {"recipient_steam_id": "76561198000000003"},
    ],
)
def test_normal_send_proof_rejects_every_bound_identity_mismatch(changes):
    item = buyer_awaiting(revision=2)
    store = RecordingStore(item)
    adapter = RecordingSendAdapter()
    coordinator, _ = normal_send_coordinator(store, adapter)
    proof = coordinator.read_send_authority(item)
    changed = StoredDelivery(replace(item.snapshot, **changes), item.revision)
    store.current = changed

    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="send_authority_proof_mismatch"):
        coordinator.send_offer_with_authority(changed, proof)
    assert adapter.calls == []
    assert store.advance_count == 0


def test_result_unknown_recovery_is_exact_read_only_and_binds_offer_and_seller():
    item = buyer_result_unknown(revision=4)
    store = RecordingStore(item)
    read_adapter = SpyAdapter(
        {PlatformCapability.READ_OFFER_STATE},
        success_factory(
            OfferStateEvidence("offer-exact", "76561198000000002")
        ),
    )
    send_adapter = RecordingSendAdapter(
        error=AssertionError("read-only recovery must not invoke SEND")
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=1.0,
        allow_writes=True,
        clock=sequence_clock(12.0),
    )

    result = coordinator.recover_result_unknown_readonly(item)

    assert len(read_adapter.calls) == 1
    assert send_adapter.calls == []
    assert result.persisted is True
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_SENT
    assert result.after.snapshot.steam_tradeoffer_id == "offer-exact"
    assert result.after.snapshot.counterparty_steam_id == "76561198000000002"
    assert result.after.snapshot.offer_sent_at == 12.0
    assert result.after.snapshot.delivery_error is None


@pytest.mark.parametrize(
    "status,expected_result",
    [
        (PlatformResultStatus.RESULT_UNKNOWN, AutoOfferResult.WAITING),
        (PlatformResultStatus.MALFORMED, AutoOfferResult.BLOCKED),
    ],
)
def test_result_unknown_readonly_recovery_missing_or_malformed_never_writes(
    status,
    expected_result,
):
    item = buyer_result_unknown()
    store = RecordingStore(item)
    read_adapter = SpyAdapter(
        {PlatformCapability.READ_OFFER_STATE},
        lambda request: PlatformResult(request, status, "order_not_proven"),
    )
    send_adapter = RecordingSendAdapter(
        error=AssertionError("read-only recovery must not invoke SEND")
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_OFFER_STATE: read_adapter,
            PlatformCapability.SEND_OFFER: send_adapter,
        },
        timeout_seconds=1.0,
        allow_writes=True,
    )

    result = coordinator.recover_result_unknown_readonly(item)

    assert result.after == item
    assert result.persisted is False
    assert result.decision.result is expected_result
    assert len(read_adapter.calls) == 1
    assert send_adapter.calls == []


def _forged_send_success(request, evidence):
    forged = object.__new__(PlatformResult)
    object.__setattr__(forged, "request", request)
    object.__setattr__(forged, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged, "detail", None)
    object.__setattr__(forged, "evidence", evidence)
    return forged


@pytest.mark.parametrize("kind", ["timeout", "exception", "malformed", "failure", "bare", "wrong"])
def test_every_unproven_invoked_send_outcome_becomes_result_unknown_without_retry(kind):
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    store = RecordingStore(item)
    if kind == "timeout":
        adapter = RecordingSendAdapter(error=PlatformAdapterTimeoutError("secret"))
    elif kind == "exception":
        adapter = RecordingSendAdapter(error=RuntimeError("secret"))
    elif kind == "malformed":
        adapter = RecordingSendAdapter(lambda request: object())
    elif kind == "failure":
        adapter = RecordingSendAdapter(
            lambda request: PlatformResult(
                request,
                PlatformResultStatus.FAILURE,
                "send_failed",
            )
        )
    elif kind == "bare":
        adapter = RecordingSendAdapter(
            lambda request: _forged_send_success(request, None)
        )
    else:
        adapter = RecordingSendAdapter(
            lambda request: _forged_send_success(
                request, OfferStateEvidence("offer-1", "76561198000000002")
            )
        )

    coordinator, direction_adapter = normal_send_coordinator(
        store,
        adapter,
        clock=sequence_clock(10.0),
    )
    proof = coordinator.read_send_authority(item)
    result = coordinator.send_offer_with_authority(item, proof)

    assert len(direction_adapter.calls) == 1
    assert len(adapter.calls) == 1
    assert result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED
    assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert result.after.snapshot.delivery_error == "write_result_unknown"
    assert result.after.snapshot.steam_tradeoffer_id is None
    assert result.after.revision == item.revision + 2

    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="read_step_not_available"):
        DeliveryCoordinator(
            store,
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
            allow_writes=True,
            clock=sequence_clock(20.0),
        ).step(result.after)
    assert len(adapter.calls) == 1


def test_missing_send_adapter_and_invalid_preflight_clock_never_record_attempt():
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    store = RecordingStore(item)
    coordinator, _ = normal_send_coordinator(store, clock=sequence_clock(10.0))
    proof = coordinator.read_send_authority(item)
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="send_offer_adapter_required"):
        coordinator.send_offer_with_authority(item, proof)
    assert store.advance_count == 0

    adapter = RecordingSendAdapter()
    store = RecordingStore(item)
    coordinator, _ = normal_send_coordinator(
        store,
        adapter,
        clock=sequence_clock(nan),
    )
    proof = coordinator.read_send_authority(item)
    with pytest.raises(ReadOnlyCoordinatorError, match="invalid_clock_value"):
        coordinator.send_offer_with_authority(item, proof)
    assert store.advance_count == 0
    assert adapter.calls == []


def test_stale_store_preflight_never_records_attempt_or_calls_send():
    from app.auto_offer.coordinator import DeliveryCoordinator

    supplied = buyer_awaiting(revision=1)
    persisted = buyer_awaiting(revision=2)
    store = RecordingStore(persisted)
    adapter = RecordingSendAdapter()
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.SEND_OFFER: adapter},
        timeout_seconds=1.0,
        allow_writes=True,
        clock=sequence_clock(10.0),
    )
    with pytest.raises(ReadOnlyCoordinatorConflictError, match="persisted_delivery_mismatch"):
        coordinator.step(supplied)
    assert store.advance_count == 0
    assert adapter.calls == []


def test_attempt_persistence_failure_happens_before_and_prevents_external_call():
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    store = RecordingStore(item, fail_on_advance=1)
    adapter = RecordingSendAdapter()
    coordinator, _ = normal_send_coordinator(
        store,
        adapter,
        clock=sequence_clock(10.0),
    )
    proof = coordinator.read_send_authority(item)
    with pytest.raises(ReadOnlyCoordinatorError, match="store_advance_failed"):
        coordinator.send_offer_with_authority(item, proof)
    assert adapter.calls == []
    assert store.current == item


def test_success_persistence_failure_leaves_durable_attempt_and_never_resends():
    from app.auto_offer.adapters import SendOfferEvidence
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    store = RecordingStore(item, fail_on_advance=2)
    adapter = RecordingSendAdapter(
        lambda request: PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=SendOfferEvidence("offer-1"),
        )
    )
    coordinator, _ = normal_send_coordinator(
        store,
        adapter,
        clock=sequence_clock(10.0),
    )
    proof = coordinator.read_send_authority(item)
    with pytest.raises(ReadOnlyCoordinatorError, match="store_advance_failed"):
        coordinator.send_offer_with_authority(item, proof)
    assert len(adapter.calls) == 1
    assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED
    assert store.current.snapshot.offer_attempted_at == 10.0

    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="read_step_not_available"):
        DeliveryCoordinator(
            store,
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
            allow_writes=True,
            clock=sequence_clock(20.0),
        ).step(store.current)
    assert len(adapter.calls) == 1


def test_result_unknown_persistence_failure_leaves_durable_attempt_and_never_resends():
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = buyer_awaiting()
    store = RecordingStore(item, fail_on_advance=2)
    adapter = RecordingSendAdapter(error=PlatformAdapterTimeoutError("secret"))
    coordinator, _ = normal_send_coordinator(
        store,
        adapter,
        clock=sequence_clock(10.0),
    )
    proof = coordinator.read_send_authority(item)
    with pytest.raises(ReadOnlyCoordinatorError, match="store_advance_failed"):
        coordinator.send_offer_with_authority(item, proof)
    assert len(adapter.calls) == 1
    assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_ATTEMPTED

    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="read_step_not_available"):
        DeliveryCoordinator(
            store,
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
            allow_writes=True,
            clock=sequence_clock(20.0),
        ).step(store.current)
    assert len(adapter.calls) == 1


def test_canary_post_call_clock_failure_or_regression_becomes_result_unknown():
    from app.auto_offer.adapters import SendOfferEvidence
    from app.auto_offer.coordinator import DeliveryCoordinator

    for clock in (sequence_clock(10.0), sequence_clock(10.0, 9.0)):
        item = buyer_awaiting()
        store = RecordingStore(item)
        adapter = RecordingSendAdapter(
            lambda request: PlatformResult(
                request,
                PlatformResultStatus.SUCCESS,
                evidence=SendOfferEvidence("offer-1"),
            )
        )
        result = DeliveryCoordinator(
            store,
            {PlatformCapability.SEND_OFFER: adapter},
            timeout_seconds=1.0,
            allow_writes=True,
            clock=clock,
            expected_trade_offer_counterparty_steam_id="76561198000000002",
            expected_trade_offer_is_our_offer=True,
        ).step(item)
        assert len(adapter.calls) == 1
        assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
        assert result.after.snapshot.delivery_error == "write_result_unknown"


def test_normal_confirmation_snapshot_alone_blocks_before_attempt_or_adapter():
    item = buyer_confirmation_required()
    store = RecordingStore(item)
    confirm_adapter = RecordingConfirmAdapter(
        lambda _request: (_ for _ in ()).throw(
            AssertionError("snapshot-only CONFIRM must not execute")
        )
    )
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="normal_confirmation_authority_required",
    ):
        coordinator.step(item)

    assert store.advance_count == 0
    assert buff_adapter.calls == []
    assert steam_adapter.calls == []
    assert confirm_adapter.calls == []


def test_normal_confirmation_authority_requires_durable_seller():
    item = buyer_confirmation_required(counterparty=None)
    store = RecordingStore(item)
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(store)

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="normal_confirmation_authority_not_available",
    ):
        coordinator.read_confirmation_authority(item)

    assert buff_adapter.calls == []
    assert steam_adapter.calls == []
    assert store.advance_count == 0


@pytest.mark.parametrize(
    "buff_evidence",
    [
        OfferStateEvidence("offer-other", CONFIRM_COUNTERPARTY),
        OfferStateEvidence("offer-1", "76561198000000003"),
    ],
)
def test_normal_confirmation_authority_rejects_buff_offer_or_seller_mismatch(
    buff_evidence,
):
    item = buyer_confirmation_required()
    store = RecordingStore(item)
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(
        store,
        buff_evidence=buff_evidence,
    )

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_buff_identity_mismatch",
    ):
        coordinator.read_confirmation_authority(item)

    assert len(buff_adapter.calls) == 1
    assert steam_adapter.calls == []
    assert store.advance_count == 0


def test_normal_confirmation_transient_buff_evidence_mints_no_proof_or_steam_read():
    item = buyer_confirmation_required()
    store = RecordingStore(item)
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(
        store,
        buff_result_factory=lambda request: PlatformResult(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "order_not_proven",
        ),
    )

    result = coordinator.read_confirmation_authority(item)

    assert result.proof is None
    assert result.steam_result is None
    assert len(buff_adapter.calls) == 1
    assert steam_adapter.calls == []
    assert store.advance_count == 0


@pytest.mark.parametrize(
    "steam_evidence",
    [
        exact_confirmation_steam_evidence(offer_id="offer-other"),
        exact_confirmation_steam_evidence(account_id="76561198000000003"),
        exact_confirmation_steam_evidence(counterparty="76561198000000003"),
        exact_confirmation_steam_evidence(is_our_offer=False),
    ],
)
def test_normal_confirmation_authority_rejects_steam_identity_mismatch(
    steam_evidence,
):
    item = buyer_confirmation_required()
    store = RecordingStore(item)
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(
        store,
        steam_evidence=steam_evidence,
    )

    result = coordinator.read_confirmation_authority(item)

    assert len(buff_adapter.calls) == len(steam_adapter.calls) == 1
    assert result.proof is None
    assert result.steam_result.decision.result is AutoOfferResult.BLOCKED
    assert result.steam_result.persisted is False
    assert store.advance_count == 0


def test_normal_confirmation_authority_safe_read_transition_and_outgoing_items_never_confirm():
    item = buyer_confirmation_required()
    store = RecordingStore(item)
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        steam_evidence=exact_confirmation_steam_evidence(
            lifecycle=SteamTradeOfferLifecycle.ACTIVE
        ),
    )

    result = coordinator.read_confirmation_authority(item)

    assert result.proof is None
    assert result.steam_result.persisted is True
    assert result.steam_result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED

    item = buyer_confirmation_required()
    store = RecordingStore(item)
    outgoing = (TradeOfferItemEvidence(730, "2", "outgoing-asset", 1),)
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        steam_evidence=exact_confirmation_steam_evidence(items_to_give=outgoing),
    )
    result = coordinator.read_confirmation_authority(item)
    assert result.proof is None
    assert result.steam_result.persisted is False
    assert store.advance_count == 0


def test_normal_confirmation_proof_is_exact_opaque_single_use_and_process_local():
    item = buyer_confirmation_required(revision=7)
    store = RecordingStore(item)
    confirm_adapter = RecordingConfirmAdapter(
        lambda request: PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=ConfirmOfferEvidence("offer-1", "76561198000000001"),
        )
    )
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )

    authority = coordinator.read_confirmation_authority(item)
    proof = authority.proof
    assert repr(proof) == "<opaque normal confirmation proof>"
    assert (
        proof.purchase_id,
        proof.buff_order_id,
        proof.account_id,
        proof.recipient_steam_id,
        proof.revision,
        proof.steam_tradeoffer_id,
        proof.counterparty_steam_id,
    ) == (
        item.snapshot.purchase_id,
        item.snapshot.buff_order_id,
        item.snapshot.account_id,
        item.snapshot.recipient_steam_id,
        item.revision,
        item.snapshot.steam_tradeoffer_id,
        item.snapshot.counterparty_steam_id,
    )
    with pytest.raises(TypeError, match="normal_confirmation_proof_not_serializable"):
        pickle.dumps(proof)
    with pytest.raises(TypeError, match="normal_confirmation_proof_not_serializable"):
        copy.copy(proof)

    restarted, _, _ = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )
    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_authority_proof_required",
    ):
        restarted.confirm_offer_with_authority(item, proof)

    result = coordinator.confirm_offer_with_authority(item, proof)
    assert result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert len(confirm_adapter.calls) == 1
    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_authority_proof_required",
    ):
        coordinator.confirm_offer_with_authority(item, proof)
    assert len(confirm_adapter.calls) == 1


def test_normal_confirmation_stale_revision_blocks_before_attempt_or_confirm():
    item = buyer_confirmation_required(revision=2)
    store = RecordingStore(item)
    confirm_adapter = RecordingConfirmAdapter()
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )
    proof = coordinator.read_confirmation_authority(item).proof
    store.current = StoredDelivery(item.snapshot, item.revision + 1)

    with pytest.raises(
        ReadOnlyCoordinatorConflictError,
        match="persisted_delivery_mismatch",
    ):
        coordinator.confirm_offer_with_authority(item, proof)
    assert store.advance_count == 0
    assert confirm_adapter.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"purchase_id": "purchase-other"},
        {"buff_order_id": "buff-order-other"},
        {"account_id": "account-other"},
        {"recipient_steam_id": "76561198000000003"},
        {"steam_tradeoffer_id": "offer-other"},
        {"counterparty_steam_id": "76561198000000003"},
    ],
)
def test_normal_confirmation_proof_rejects_identity_offer_or_seller_mismatch(changes):
    item = buyer_confirmation_required(revision=3)
    store = RecordingStore(item)
    confirm_adapter = RecordingConfirmAdapter()
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )
    proof = coordinator.read_confirmation_authority(item).proof
    changed = StoredDelivery(replace(item.snapshot, **changes), item.revision)
    store.current = changed

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="confirmation_authority_proof_mismatch",
    ):
        coordinator.confirm_offer_with_authority(changed, proof)
    assert store.advance_count == 0
    assert confirm_adapter.calls == []


@pytest.mark.parametrize(
    "platform_status,expected_status,expected_revision_delta",
    [
        (PlatformResultStatus.SUCCESS, DeliveryStatus.OFFER_CONFIRMED, 2),
        (PlatformResultStatus.RESULT_UNKNOWN, DeliveryStatus.RESULT_UNKNOWN, 2),
        (PlatformResultStatus.FAILURE, DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED, 1),
    ],
)
def test_normal_confirmation_attempt_precedes_exactly_one_call_and_preserves_results(
    platform_status,
    expected_status,
    expected_revision_delta,
):
    item = buyer_confirmation_required(revision=4)
    store = RecordingStore(item)

    def result_factory(request):
        assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
        if platform_status is PlatformResultStatus.SUCCESS:
            return PlatformResult(
                request,
                platform_status,
                evidence=ConfirmOfferEvidence("offer-1", "76561198000000001"),
            )
        return PlatformResult(request, platform_status, "confirmation_not_proven")

    confirm_adapter = RecordingConfirmAdapter(result_factory)
    coordinator, _, _ = normal_confirmation_coordinator(
        store,
        confirm_adapter=confirm_adapter,
    )
    proof = coordinator.read_confirmation_authority(item).proof
    result = coordinator.confirm_offer_with_authority(item, proof)

    assert len(confirm_adapter.calls) == 1
    assert result.after.snapshot.delivery_status is expected_status
    assert result.after.revision == item.revision + expected_revision_delta
    if platform_status is PlatformResultStatus.RESULT_UNKNOWN:
        assert result.after.snapshot.delivery_error == "write_result_unknown"
    with pytest.raises(ReadOnlyCoordinatorBlockedError):
        coordinator.confirm_offer_with_authority(item, proof)
    assert len(confirm_adapter.calls) == 1


def bound_confirmation_unknown(revision=8):
    required = buyer_confirmation_required(revision=revision)
    return StoredDelivery(
        replace(
            required.snapshot,
            delivery_status=DeliveryStatus.RESULT_UNKNOWN,
            delivery_error="write_result_unknown",
        ),
        revision,
    )


@pytest.mark.parametrize(
    "lifecycle,expected_status,persisted",
    [
        (SteamTradeOfferLifecycle.ACTIVE, DeliveryStatus.OFFER_CONFIRMED, True),
        (SteamTradeOfferLifecycle.ACCEPTED, DeliveryStatus.OFFER_CONFIRMED, True),
        (SteamTradeOfferLifecycle.IN_ESCROW, DeliveryStatus.OFFER_CONFIRMED, True),
        (
            SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION,
            DeliveryStatus.RESULT_UNKNOWN,
            False,
        ),
    ],
)
def test_bound_confirmation_unknown_recovery_is_steam_read_only(
    lifecycle,
    expected_status,
    persisted,
):
    item = bound_confirmation_unknown()
    store = RecordingStore(item)
    confirm_adapter = RecordingConfirmAdapter(
        lambda _request: (_ for _ in ()).throw(
            AssertionError("read-only recovery must never CONFIRM")
        )
    )
    coordinator, buff_adapter, steam_adapter = normal_confirmation_coordinator(
        store,
        steam_evidence=exact_confirmation_steam_evidence(lifecycle=lifecycle),
        confirm_adapter=confirm_adapter,
    )

    result = coordinator.recover_confirmation_result_unknown_readonly(item)

    assert buff_adapter.calls == []
    assert len(steam_adapter.calls) == 1
    assert confirm_adapter.calls == []
    assert result.persisted is persisted
    assert result.after.snapshot.delivery_status is expected_status
    if expected_status is DeliveryStatus.OFFER_CONFIRMED:
        assert result.after.snapshot.delivery_error is None


@pytest.mark.parametrize("malformed", [False, True])
def test_bound_confirmation_unknown_recovery_identity_or_malformed_is_blocked(malformed):
    item = bound_confirmation_unknown()
    store = RecordingStore(item)
    steam_evidence = exact_confirmation_steam_evidence(
        counterparty="76561198000000003"
    )
    result_factory = (lambda _request: object()) if malformed else None
    coordinator, _, steam_adapter = normal_confirmation_coordinator(
        store,
        steam_evidence=steam_evidence,
        steam_result_factory=result_factory,
    )

    result = coordinator.recover_confirmation_result_unknown_readonly(item)

    assert len(steam_adapter.calls) == 1
    assert result.persisted is False
    assert result.after == item
    assert result.decision.result is AutoOfferResult.BLOCKED


def test_write_authority_does_not_import_or_wire_task007_executor():
    from pathlib import Path

    path = Path(__file__).parents[1] / "app" / "auto_offer" / "coordinator.py"
    text = path.read_text(encoding="utf-8")
    assert "DeliveryExecutor" not in text
    assert "executor" not in text.lower()
    assert "buyer_send_offer" not in text
    assert "POST" not in text
    assert "retry" not in text.lower()


SELLER_RECIPIENT = "76561198000000001"
SELLER_COUNTERPARTY = "76561198000000002"
SELLER_GOODS_ID = 73001
SELLER_ASSET_ID = "seller-asset-1"


def seller_offer_confirmed(
    revision=7,
    *,
    offer_id="offer-1",
    counterparty=SELLER_COUNTERPARTY,
):
    snapshot = make_snapshot(
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryMode.SELLER_SENDS_OFFER,
        steam_tradeoffer_id=offer_id,
        counterparty_steam_id=counterparty,
    )
    return make_delivery(
        replace(snapshot, recipient_steam_id=SELLER_RECIPIENT),
        revision=revision,
    )


def exact_seller_item_evidence(**changes):
    value = SellerOrderItemEvidence(
        buff_order_id="buff-order-1",
        steam_tradeoffer_id="offer-1",
        recipient_steam_id=SELLER_RECIPIENT,
        counterparty_steam_id=SELLER_COUNTERPARTY,
        goods_id=SELLER_GOODS_ID,
        seller_assetid=SELLER_ASSET_ID,
    )
    return replace(value, **changes)


def exact_seller_accept_steam_evidence(
    *,
    lifecycle=SteamTradeOfferLifecycle.ACTIVE,
    offer_id="offer-1",
    account_id=SELLER_RECIPIENT,
    counterparty=SELLER_COUNTERPARTY,
    is_our_offer=False,
    items_to_give=(),
    items_to_receive=None,
):
    if items_to_receive is None:
        items_to_receive = (
            TradeOfferItemEvidence(730, "2", SELLER_ASSET_ID, 1),
        )
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id=offer_id,
        account_steam_id=account_id,
        counterparty_steam_id=counterparty,
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=items_to_give,
        items_to_receive=items_to_receive,
    )


def seller_authority_coordinator(
    store,
    *,
    buff_evidence=None,
    steam_evidence=None,
    buff_result_factory=None,
    steam_result_factory=None,
    accept_adapter=None,
    allow_accept_writes=False,
    write_guard=None,
    order_log=None,
):
    from app.auto_offer.coordinator import DeliveryCoordinator

    buff_evidence = buff_evidence or exact_seller_item_evidence()
    steam_evidence = steam_evidence or exact_seller_accept_steam_evidence()

    def buff_success(request):
        if order_log is not None:
            order_log.append("buff")
        return PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=buff_evidence,
        )

    def steam_success(request):
        if order_log is not None:
            order_log.append("steam")
        return PlatformResult(
            request,
            PlatformResultStatus.SUCCESS,
            evidence=steam_evidence,
        )

    buff_adapter = SpyAdapter(
        {PlatformCapability.READ_SELLER_OFFER_ITEM},
        buff_result_factory or buff_success,
    )
    steam_adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        steam_result_factory or steam_success,
    )
    adapters = {
        PlatformCapability.READ_SELLER_OFFER_ITEM: buff_adapter,
        PlatformCapability.READ_STEAM_TRADE_OFFER: steam_adapter,
    }
    if accept_adapter is not None:
        adapters[PlatformCapability.ACCEPT_OFFER] = accept_adapter
    coordinator = DeliveryCoordinator(
        store,
        adapters,
        timeout_seconds=1.0,
        allow_writes=allow_accept_writes,
        allow_accept_writes=allow_accept_writes,
        write_guard=write_guard,
    )
    return coordinator, buff_adapter, steam_adapter


def test_seller_authority_reads_fresh_buff_before_steam_and_mints_exact_proof():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    order = []
    coordinator, buff_adapter, steam_adapter = seller_authority_coordinator(
        store,
        order_log=order,
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)
    proof = result.proof

    assert order == ["buff", "steam"]
    assert len(buff_adapter.calls) == len(steam_adapter.calls) == 1
    assert buff_adapter.calls[0].steam_tradeoffer_id == "offer-1"
    assert buff_adapter.calls[0].counterparty_steam_id == SELLER_COUNTERPARTY
    assert buff_adapter.calls[0].host_goods_id == SELLER_GOODS_ID
    assert proof is coordinator._seller_accept_proof
    assert repr(proof) == "<opaque seller accept proof>"
    assert (
        proof.purchase_id,
        proof.buff_order_id,
        proof.account_id,
        proof.recipient_steam_id,
        proof.revision,
        proof.steam_tradeoffer_id,
        proof.counterparty_steam_id,
        proof.host_goods_id,
        proof.seller_assetid,
    ) == (
        item.snapshot.purchase_id,
        item.snapshot.buff_order_id,
        item.snapshot.account_id,
        SELLER_RECIPIENT,
        item.revision,
        "offer-1",
        SELLER_COUNTERPARTY,
        SELLER_GOODS_ID,
        SELLER_ASSET_ID,
    )
    assert store.advance_count == 0


def test_seller_accept_proof_is_opaque_process_local_and_single_current():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, _ = seller_authority_coordinator(store)
    first = coordinator.read_seller_accept_authority(
        item,
        SELLER_GOODS_ID,
    ).proof

    with pytest.raises(TypeError, match="seller_accept_proof_not_serializable"):
        pickle.dumps(first)
    with pytest.raises(TypeError, match="seller_accept_proof_not_serializable"):
        copy.copy(first)
    with pytest.raises(TypeError, match="seller_accept_proof_not_serializable"):
        copy.deepcopy(first)

    second = coordinator.read_seller_accept_authority(
        item,
        SELLER_GOODS_ID,
    ).proof
    restarted, _, _ = seller_authority_coordinator(store)
    assert second is not first
    assert coordinator._seller_accept_proof is second
    assert restarted._seller_accept_proof is None


@pytest.mark.parametrize(
    "status",
    [PlatformResultStatus.RESULT_UNKNOWN, PlatformResultStatus.TIMEOUT],
)
def test_seller_authority_transient_buff_result_mints_no_proof_or_steam_read(
    status,
):
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, buff_adapter, steam_adapter = seller_authority_coordinator(
        store,
        buff_result_factory=lambda request: PlatformResult(
            request,
            status,
            "order_not_proven",
        ),
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert result.proof is None
    assert result.steam_result is None
    assert len(buff_adapter.calls) == 1
    assert steam_adapter.calls == []
    assert coordinator._seller_accept_proof is None


@pytest.mark.parametrize(
    "steam_evidence",
    [
        exact_seller_accept_steam_evidence(
            items_to_give=(
                TradeOfferItemEvidence(730, "2", "outgoing-asset", 1),
            )
        ),
        exact_seller_accept_steam_evidence(
            items_to_receive=(
                TradeOfferItemEvidence(730, "2", SELLER_ASSET_ID, 1),
                TradeOfferItemEvidence(730, "2", "seller-asset-2", 1),
            )
        ),
        exact_seller_accept_steam_evidence(
            items_to_give=(
                TradeOfferItemEvidence(730, "2", "outgoing-asset", 1),
            ),
            items_to_receive=(),
        ),
        exact_seller_accept_steam_evidence(
            items_to_receive=(
                TradeOfferItemEvidence(440, "2", SELLER_ASSET_ID, 1),
            )
        ),
        exact_seller_accept_steam_evidence(
            items_to_receive=(
                TradeOfferItemEvidence(730, "2", SELLER_ASSET_ID, 2),
            )
        ),
        exact_seller_accept_steam_evidence(
            items_to_receive=(
                TradeOfferItemEvidence(730, "2", "wrong-asset", 1),
            )
        ),
        exact_seller_accept_steam_evidence(is_our_offer=True),
        exact_seller_accept_steam_evidence(offer_id="offer-2"),
        exact_seller_accept_steam_evidence(
            account_id="76561198000000003"
        ),
        exact_seller_accept_steam_evidence(
            counterparty="76561198000000003"
        ),
        exact_seller_accept_steam_evidence(
            lifecycle=SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
        ),
    ],
)
def test_seller_authority_steam_identity_direction_or_item_gap_mints_no_proof(
    steam_evidence,
):
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, steam_adapter = seller_authority_coordinator(
        store,
        steam_evidence=steam_evidence,
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert len(steam_adapter.calls) == 1
    assert result.proof is None
    assert coordinator._seller_accept_proof is None


def test_seller_authority_steam_timeout_mints_no_proof():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, steam_adapter = seller_authority_coordinator(
        store,
        steam_result_factory=lambda request: PlatformResult(
            request,
            PlatformResultStatus.TIMEOUT,
            "read_timeout",
        ),
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert len(steam_adapter.calls) == 1
    assert result.proof is None
    assert coordinator._seller_accept_proof is None


@pytest.mark.parametrize(
    "lifecycle",
    [SteamTradeOfferLifecycle.ACCEPTED, SteamTradeOfferLifecycle.IN_ESCROW],
)
def test_external_accepted_or_escrow_progresses_readonly_without_proof(lifecycle):
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, _ = seller_authority_coordinator(
        store,
        steam_evidence=exact_seller_accept_steam_evidence(lifecycle=lifecycle),
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert result.proof is None
    assert result.steam_result.persisted is True
    assert (
        result.steam_result.after.snapshot.delivery_status
        is DeliveryStatus.AWAITING_INVENTORY
    )
    assert store.advance_count == 1


def test_terminal_seller_offer_progresses_readonly_and_mints_no_proof():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, _ = seller_authority_coordinator(
        store,
        steam_evidence=exact_seller_accept_steam_evidence(
            lifecycle=SteamTradeOfferLifecycle.DECLINED
        ),
    )

    result = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert result.proof is None
    assert result.steam_result.persisted is True
    assert (
        result.steam_result.after.snapshot.delivery_status
        is DeliveryStatus.OFFER_TERMINATED
    )


def test_store_revision_offer_or_counterparty_change_invalidates_seller_proof():
    item = seller_offer_confirmed()
    for changed in (
        StoredDelivery(item.snapshot, item.revision + 1),
        StoredDelivery(
            replace(item.snapshot, steam_tradeoffer_id="offer-2"),
            item.revision,
        ),
        StoredDelivery(
            replace(
                item.snapshot,
                counterparty_steam_id="76561198000000003",
            ),
            item.revision,
        ),
    ):
        store = RecordingStore(item)
        coordinator, _, _ = seller_authority_coordinator(store)
        assert coordinator.read_seller_accept_authority(
            item,
            SELLER_GOODS_ID,
        ).proof is not None
        store.current = changed
        with pytest.raises(ReadOnlyCoordinatorConflictError):
            coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)
        assert coordinator._seller_accept_proof is None


def test_intervening_coordinator_operation_invalidates_seller_proof():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, _ = seller_authority_coordinator(store)
    assert coordinator.read_seller_accept_authority(
        item,
        SELLER_GOODS_ID,
    ).proof is not None

    coordinator.step(item)

    assert coordinator._seller_accept_proof is None


def test_persisted_snapshot_alone_cannot_mint_seller_proof():
    from app.auto_offer.coordinator import DeliveryCoordinator

    item = seller_offer_confirmed()
    steam_adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        success_factory(exact_seller_accept_steam_evidence()),
    )
    coordinator = DeliveryCoordinator(
        RecordingStore(item),
        {PlatformCapability.READ_STEAM_TRADE_OFFER: steam_adapter},
        timeout_seconds=1.0,
    )

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="seller_accept_buff_adapter_required",
    ):
        coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID)

    assert coordinator._seller_accept_proof is None
    assert steam_adapter.calls == []


def test_c3b2_accept_is_explicitly_enabled_and_snapshot_step_stays_readonly():
    import app.auto_offer.coordinator as coordinator_module
    from app.auto_offer.coordinator import DeliveryCoordinator

    assert (
        PlatformCapability.ACCEPT_OFFER
        in coordinator_module._WRITE_CAPABILITIES
    )
    assert hasattr(
        coordinator_module.DeliveryCoordinator,
        "accept_offer_with_authority",
    )
    accept_adapter = SpyAdapter(
        {PlatformCapability.ACCEPT_OFFER},
        error=AssertionError("ordinary step must not ACCEPT"),
    )
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    coordinator, _, steam_adapter = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )

    result = coordinator.step(item)

    assert result.persisted is False
    assert result.after == item
    assert len(steam_adapter.calls) == 1
    assert accept_adapter.calls == []

    with pytest.raises(ReadOnlyCoordinatorError, match="accept_writes_require_allow_writes"):
        DeliveryCoordinator(
            store,
            {},
            timeout_seconds=1.0,
            allow_accept_writes=True,
        )


def test_seller_proof_consumption_persists_attempt_before_exactly_one_accept():
    item = seller_offer_confirmed()
    events = []
    store = RecordingStore(item, events=events)

    def accept_unknown(request):
        assert store.current.snapshot.delivery_status is DeliveryStatus.OFFER_ACCEPT_ATTEMPTED
        events.append(("accept", request.revision, request.counterparty_steam_id))
        return PlatformResult(
            request,
            PlatformResultStatus.RESULT_UNKNOWN,
            "write_result_unknown",
        )

    accept_adapter = SpyAdapter(
        {PlatformCapability.ACCEPT_OFFER},
        accept_unknown,
    )
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(
        item,
        SELLER_GOODS_ID,
    ).proof

    result = coordinator.accept_offer_with_authority(item, proof)

    assert result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_ACCEPT_ATTEMPTED
    assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert result.after.snapshot.delivery_error == "write_result_unknown"
    assert len(accept_adapter.calls) == 1
    request = accept_adapter.calls[0]
    assert request.revision == result.attempted.revision
    assert request.steam_tradeoffer_id == item.snapshot.steam_tradeoffer_id
    assert request.counterparty_steam_id == SELLER_COUNTERPARTY
    assert request.host_goods_id is None
    assert events.index(("accept", request.revision, SELLER_COUNTERPARTY)) > next(
        index
        for index, event in enumerate(events)
        if event[:3]
        == (
            "advance",
            DeliveryStatus.OFFER_CONFIRMED,
            DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        )
    )

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="seller_accept_authority_proof_required",
    ):
        coordinator.accept_offer_with_authority(item, proof)
    assert len(accept_adapter.calls) == 1


def test_accept_preflight_failure_stays_attempted_and_never_reuses_proof():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    accept_adapter = SpyAdapter(
        {PlatformCapability.ACCEPT_OFFER},
        lambda request: PlatformResult(
            request,
            PlatformResultStatus.FAILURE,
            "write_preflight_failed",
        ),
    )
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID).proof

    result = coordinator.accept_offer_with_authority(item, proof)

    assert result.after == result.attempted
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_ACCEPT_ATTEMPTED
    with pytest.raises(ReadOnlyCoordinatorBlockedError):
        coordinator.accept_offer_with_authority(item, proof)
    assert len(accept_adapter.calls) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"purchase_id": "other-purchase"},
        {"buff_order_id": "other-order"},
        {"account_id": "other-account"},
        {"recipient_steam_id": "76561198000000003"},
        {"steam_tradeoffer_id": "offer-2"},
        {"counterparty_steam_id": "76561198000000003"},
    ],
)
def test_seller_accept_proof_identity_mismatch_blocks_before_attempted_cas(changed):
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    accept_adapter = SpyAdapter({PlatformCapability.ACCEPT_OFFER})
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID).proof
    changed_item = StoredDelivery(replace(item.snapshot, **changed), item.revision)
    store.current = changed_item
    advances = store.advance_count

    with pytest.raises(ReadOnlyCoordinatorBlockedError):
        coordinator.accept_offer_with_authority(changed_item, proof)

    assert store.advance_count == advances
    assert accept_adapter.calls == []


def test_seller_accept_proof_stale_revision_blocks_before_attempted_cas():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    accept_adapter = SpyAdapter({PlatformCapability.ACCEPT_OFFER})
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID).proof
    store.current = StoredDelivery(item.snapshot, item.revision + 1)

    with pytest.raises(
        ReadOnlyCoordinatorConflictError,
        match="persisted_delivery_mismatch",
    ):
        coordinator.accept_offer_with_authority(item, proof)

    assert store.advance_count == 0
    assert accept_adapter.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("host_goods_id", SELLER_GOODS_ID + 1), ("seller_assetid", "other-asset")],
)
def test_seller_accept_proof_goods_or_asset_mismatch_blocks_before_cas(field, value):
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    accept_adapter = SpyAdapter({PlatformCapability.ACCEPT_OFFER})
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID).proof
    object.__setattr__(proof, field, value)

    with pytest.raises(
        ReadOnlyCoordinatorBlockedError,
        match="seller_accept_authority_proof_mismatch",
    ):
        coordinator.accept_offer_with_authority(item, proof)
    assert store.advance_count == 0
    assert accept_adapter.calls == []


def seller_accept_result_unknown(lifecycle=SteamTradeOfferLifecycle.ACTIVE):
    item = seller_offer_confirmed()
    snapshot = replace(
        item.snapshot,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    return StoredDelivery(snapshot, item.revision + 2), exact_seller_accept_steam_evidence(
        lifecycle=lifecycle
    )


@pytest.mark.parametrize(
    ("lifecycle", "expected_status", "persisted"),
    [
        (SteamTradeOfferLifecycle.ACTIVE, DeliveryStatus.RESULT_UNKNOWN, False),
        (SteamTradeOfferLifecycle.ACCEPTED, DeliveryStatus.AWAITING_INVENTORY, True),
        (SteamTradeOfferLifecycle.IN_ESCROW, DeliveryStatus.AWAITING_INVENTORY, True),
        (SteamTradeOfferLifecycle.DECLINED, DeliveryStatus.OFFER_TERMINATED, True),
    ],
)
def test_seller_accept_unknown_recovery_is_exact_readonly(
    lifecycle,
    expected_status,
    persisted,
):
    item, evidence = seller_accept_result_unknown(lifecycle)
    store = RecordingStore(item)
    read_adapter = SpyAdapter(
        {PlatformCapability.READ_STEAM_TRADE_OFFER},
        success_factory(evidence),
    )
    accept_adapter = SpyAdapter(
        {PlatformCapability.ACCEPT_OFFER},
        error=AssertionError("recovery must not ACCEPT"),
    )
    from app.auto_offer.coordinator import DeliveryCoordinator

    coordinator = DeliveryCoordinator(
        store,
        {
            PlatformCapability.READ_STEAM_TRADE_OFFER: read_adapter,
            PlatformCapability.ACCEPT_OFFER: accept_adapter,
        },
        timeout_seconds=1.0,
        allow_writes=True,
        allow_accept_writes=True,
    )

    result = coordinator.recover_accept_result_unknown_readonly(item)

    assert result.persisted is persisted
    assert result.after.snapshot.delivery_status is expected_status
    assert len(read_adapter.calls) == 1
    assert accept_adapter.calls == []
    if expected_status is DeliveryStatus.AWAITING_INVENTORY:
        assert result.after.snapshot.delivery_error is None


def test_restart_has_no_seller_accept_authority():
    item = seller_offer_confirmed()
    store = RecordingStore(item)
    accept_adapter = SpyAdapter({PlatformCapability.ACCEPT_OFFER})
    coordinator, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )
    proof = coordinator.read_seller_accept_authority(item, SELLER_GOODS_ID).proof
    restarted, _, _ = seller_authority_coordinator(
        store,
        accept_adapter=accept_adapter,
        allow_accept_writes=True,
    )

    with pytest.raises(ReadOnlyCoordinatorBlockedError):
        restarted.accept_offer_with_authority(item, proof)
    assert store.advance_count == 0
    assert accept_adapter.calls == []
