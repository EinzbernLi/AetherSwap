from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from app.auto_offer.adapters import (
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
)
from app.auto_offer.contracts import (
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
):
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


def coordinator_for(item, adapter=None, *, store=None, timeout=7.5, capability=None):
    store = store or SpyStore(item)
    if adapter is None:
        capability = capability or PlatformCapability.READ_DELIVERY_DIRECTION
        adapter = SpyAdapter(
            {capability},
            success_factory(DeliveryDirectionEvidence()),
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
        success_factory(DeliveryDirectionEvidence()),
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
        success_factory(DeliveryDirectionEvidence()),
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
        success_factory(OfferStateEvidence("offer-42")),
    )
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: adapter},
        timeout_seconds=4.0,
    )
    result = coordinator.step(item)
    assert len(adapter.calls) == 1
    assert adapter.calls[0].capability is PlatformCapability.READ_OFFER_STATE
    assert len(store.advance_calls) == 1
    assert result.after.snapshot.steam_tradeoffer_id == "offer-42"
    assert result.after.snapshot == result.decision.target
    assert result.after.revision == 2


def test_inventory_read_waits_without_persistence():
    item = make_delivery(
        make_snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    store = SpyStore(item)
    adapter = SpyAdapter(
        {PlatformCapability.READ_INVENTORY_STATE},
        success_factory(InventoryStateEvidence(("asset-1",), 1)),
    )
    result = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_INVENTORY_STATE: adapter},
        timeout_seconds=2.0,
    ).step(item)
    assert adapter.calls[0].capability is PlatformCapability.READ_INVENTORY_STATE
    assert result.persisted is False
    assert result.after == item
    assert result.decision.target is None
    assert result.decision.detail == "purchase_asset_not_proven"
    assert store.advance_calls == []
    assert item.snapshot.assetid is None
    assert item.snapshot.received_at is None
    assert item.snapshot.pending_receipt is True


def test_buyer_path_blocks_before_adapter_and_advance():
    item = make_delivery(make_snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.BUYER_SENDS_OFFER))
    store = SpyStore(item)
    adapter = SpyAdapter({PlatformCapability.READ_OFFER_STATE})
    coordinator = ReadOnlyDeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: adapter},
        timeout_seconds=1.0,
    )
    with pytest.raises(ReadOnlyCoordinatorBlockedError, match="write_capability_required"):
        coordinator.step(item)
    assert adapter.calls == []
    assert store.advance_calls == []


@pytest.mark.parametrize(
    "status,mode,kwargs",
    [
        (DeliveryStatus.OFFER_ATTEMPTED, DeliveryMode.BUYER_SENDS_OFFER, {"offer_attempted_at": 1.0}),
        (DeliveryStatus.OFFER_SENT, DeliveryMode.BUYER_SENDS_OFFER, {"steam_tradeoffer_id": "offer-1", "offer_attempted_at": 1.0, "offer_sent_at": 2.0}),
        (DeliveryStatus.OFFER_RECEIVED, DeliveryMode.SELLER_SENDS_OFFER, {"steam_tradeoffer_id": "offer-1"}),
        (DeliveryStatus.OFFER_CONFIRMED, DeliveryMode.SELLER_SENDS_OFFER, {"steam_tradeoffer_id": "offer-1"}),
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
    object.__setattr__(forged, "evidence", DeliveryDirectionEvidence())
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
        success_factory(DeliveryDirectionEvidence()),
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
        success_factory(DeliveryDirectionEvidence()),
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
