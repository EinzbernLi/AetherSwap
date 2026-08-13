from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)
from app.auto_offer.contracts import (
    AutoOfferResult,
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    result_blocks_next_purchase,
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from app.auto_offer.coordinator import (
    DeliveryCoordinator,
    ReadOnlyCoordinatorError,
)
from app.auto_offer.platform_readonly import SteamTradeOfferReadOnlyAdapter
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.steam_readonly_transport import SteamTradeOfferHttpReader
from app.auto_offer.store import (
    AUTO_OFFER_STORE_SCHEMA_VERSION,
    AutoOfferStore,
    StoredDelivery,
)


STEAM_ID = "76561198000000001"
COUNTERPARTY_ID = "76561197960265851"
ACCOUNT_ID_OTHER = 123
OFFER_ID = "1001"
COOKIE = f"steamLoginSecure={STEAM_ID}||token-value"


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")


class FakeSession:
    verify = True

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET: {url}")
        return self.responses.pop(0)

    def post(self, *_args, **_kwargs):
        raise AssertionError("POST is forbidden")


class MemoryStore:
    def __init__(self, current: StoredDelivery):
        self.current = current
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id):
        assert purchase_id == self.current.snapshot.purchase_id
        return self.current

    def advance(self, current, target):
        assert current == self.current
        validate_delivery_transition(current.snapshot, target)
        self.advance_calls.append((current, target))
        self.current = StoredDelivery(target, current.revision + 1)
        return self.current


class RecordingAdapter:
    def __init__(self, capability, result_factory):
        self.capabilities = frozenset({capability})
        self.result_factory = result_factory
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return self.result_factory(request)


def source_item():
    return {
        "appid": 730,
        "contextid": "2",
        "assetid": "3001",
        "amount": 1,
    }


def steam_offer_payload(state: int):
    return {
        "response": {
            "offer": {
                "tradeofferid": OFFER_ID,
                "accountid_other": ACCOUNT_ID_OTHER,
                "trade_offer_state": state,
                "is_our_offer": True,
                "items_to_give": [],
                "items_to_receive": [source_item()],
            }
        }
    }


def buyer_snapshot(
    status: DeliveryStatus,
    *,
    steam_tradeoffer_id: str | None = OFFER_ID,
    delivery_error: str | None = None,
    offer_attempted_at: float | None = 1.0,
    offer_sent_at: float | None = 2.0,
) -> DeliverySnapshot:
    snapshot = DeliverySnapshot(
        purchase_id="purchase-confirmation-1",
        buff_order_id="buff-confirmation-1",
        account_id="account-confirmation-1",
        recipient_steam_id=STEAM_ID,
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=status,
        steam_tradeoffer_id=steam_tradeoffer_id,
        offer_attempted_at=offer_attempted_at,
        offer_sent_at=offer_sent_at,
        received_at=None,
        delivery_error=delivery_error,
        pending_receipt=True,
        assetid=None,
    )
    validate_delivery_snapshot(snapshot)
    return snapshot


def delivery(status: DeliveryStatus, *, revision=5, **changes) -> StoredDelivery:
    return StoredDelivery(buyer_snapshot(status, **changes), revision)


def steam_request(current: StoredDelivery) -> PlatformRequest:
    return PlatformRequest(
        purchase_id=current.snapshot.purchase_id,
        buff_order_id=current.snapshot.buff_order_id,
        account_id=current.snapshot.account_id,
        recipient_steam_id=current.snapshot.recipient_steam_id,
        revision=current.revision,
        capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
        timeout_seconds=5.0,
        steam_tradeoffer_id=current.snapshot.steam_tradeoffer_id,
    )


def steam_evidence(lifecycle: SteamTradeOfferLifecycle, *, is_our_offer=True):
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id=OFFER_ID,
        account_steam_id=STEAM_ID,
        counterparty_steam_id=COUNTERPARTY_ID,
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=(),
        items_to_receive=(
            TradeOfferItemEvidence(
                appid=730,
                contextid="2",
                assetid="3001",
                amount=1,
            ),
        ),
    )


def steam_result(
    current: StoredDelivery,
    lifecycle: SteamTradeOfferLifecycle,
    *,
    is_our_offer=True,
) -> PlatformResult:
    return PlatformResult(
        request=steam_request(current),
        status=PlatformResultStatus.SUCCESS,
        detail="exact_offer_read",
        evidence=steam_evidence(lifecycle, is_our_offer=is_our_offer),
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (2, "active"),
        (3, "accepted"),
        (9, "created_needs_confirmation"),
    ],
)
def test_exact_http_reader_maps_only_proven_trade_offer_states(state, expected):
    session = FakeSession([FakeResponse(steam_offer_payload(state))])
    reader = SteamTradeOfferHttpReader(COOKIE, session=session)

    result = reader(OFFER_ID)

    assert result["steam_tradeoffer_id"] == OFFER_ID
    assert result["account_steam_id"] == STEAM_ID
    assert result["counterparty_steam_id"] == COUNTERPARTY_ID
    assert result["is_our_offer"] is True
    assert result["lifecycle"] == expected
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("state", [1, 4, 5, 6, 7, 8, 10, 99])
def test_exact_http_reader_keeps_other_states_unproven(state):
    reader = SteamTradeOfferHttpReader(
        COOKIE,
        session=FakeSession([FakeResponse(steam_offer_payload(state))]),
    )

    assert reader(OFFER_ID) is None


def test_readonly_adapter_types_created_needs_confirmation_evidence():
    adapter = SteamTradeOfferReadOnlyAdapter(
        lambda offer_id: {
            "steam_tradeoffer_id": offer_id,
            "account_steam_id": STEAM_ID,
            "counterparty_steam_id": COUNTERPARTY_ID,
            "is_our_offer": True,
            "lifecycle": "created_needs_confirmation",
            "items_to_give": [],
            "items_to_receive": [source_item()],
        },
        account_id="account-confirmation-1",
        recipient_steam_id=STEAM_ID,
    )
    current = delivery(DeliveryStatus.OFFER_SENT)

    result = adapter.execute(steam_request(current))

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "trade_offer_created_needs_confirmation"
    assert type(result.evidence) is SteamTradeOfferEvidence
    assert (
        result.evidence.lifecycle
        is SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
    )


@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
    ],
)
def test_confirmation_states_are_buyer_only_and_exact_offer_bound(status):
    valid = buyer_snapshot(status)
    validate_delivery_snapshot(valid)

    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(
            replace(valid, delivery_mode=DeliveryMode.SELLER_SENDS_OFFER)
        )
    with pytest.raises(DeliveryContractError):
        validate_delivery_snapshot(replace(valid, steam_tradeoffer_id=None))


def test_durable_confirmation_attempt_and_unknown_require_explicit_attempt_state():
    sent = buyer_snapshot(DeliveryStatus.OFFER_SENT)
    required = replace(
        sent,
        delivery_status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
    )
    attempted = replace(
        required,
        delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
    )
    unknown = replace(
        attempted,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )

    validate_delivery_transition(sent, required)
    validate_delivery_transition(required, attempted)
    validate_delivery_transition(attempted, unknown)

    with pytest.raises(DeliveryContractError, match="durable write-attempt"):
        validate_delivery_transition(
            sent,
            replace(
                sent,
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
            ),
        )
    with pytest.raises(DeliveryContractError, match="durable write-attempt"):
        validate_delivery_transition(
            required,
            replace(
                required,
                delivery_status=DeliveryStatus.RESULT_UNKNOWN,
                delivery_error="write_result_unknown",
            ),
        )


def test_send_and_confirmation_unknown_are_distinguished_by_bound_offer_id():
    send_attempt = buyer_snapshot(
        DeliveryStatus.OFFER_ATTEMPTED,
        steam_tradeoffer_id=None,
        offer_sent_at=None,
    )
    send_unknown = replace(
        send_attempt,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    validate_delivery_transition(send_attempt, send_unknown)
    assert send_unknown.steam_tradeoffer_id is None

    confirmation_attempt = buyer_snapshot(
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    )
    confirmation_unknown = replace(
        confirmation_attempt,
        delivery_status=DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )
    validate_delivery_transition(confirmation_attempt, confirmation_unknown)
    assert confirmation_unknown.steam_tradeoffer_id == OFFER_ID
    assert confirmation_unknown.offer_sent_at == 2.0

    with pytest.raises(DeliveryContractError, match="confirmation result_unknown"):
        validate_delivery_transition(
            confirmation_unknown,
            replace(
                confirmation_unknown,
                delivery_status=DeliveryStatus.OFFER_SENT,
                delivery_error=None,
            ),
        )


def test_store_persists_required_to_attempted_without_schema_change(tmp_path):
    assert AUTO_OFFER_STORE_SCHEMA_VERSION == 1
    store = AutoOfferStore(tmp_path / "auto_offer.db")
    store.initialize()
    try:
        initial = DeliverySnapshot(
            purchase_id="purchase-store-confirmation",
            buff_order_id="buff-store-confirmation",
            account_id="account-confirmation-1",
            recipient_steam_id=STEAM_ID,
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
        current = store.ensure_initial(initial)
        current = store.advance(
            current,
            replace(
                current.snapshot,
                delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
                delivery_status=DeliveryStatus.AWAITING_OFFER,
            ),
        )
        current = store.advance(
            current,
            replace(
                current.snapshot,
                delivery_status=DeliveryStatus.OFFER_ATTEMPTED,
                offer_attempted_at=1.0,
            ),
        )
        current = store.advance(
            current,
            replace(
                current.snapshot,
                delivery_status=DeliveryStatus.OFFER_SENT,
                steam_tradeoffer_id=OFFER_ID,
                offer_sent_at=2.0,
            ),
        )
        required = store.advance(
            current,
            replace(
                current.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
            ),
        )
        assert required in store.list_recoverable()

        attempted = store.advance(
            required,
            replace(
                required.snapshot,
                delivery_status=DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            ),
        )
        assert attempted.revision == required.revision + 1
        assert attempted in store.list_recoverable()
        assert store.get_by_purchase_id(initial.purchase_id) == attempted
    finally:
        store.close()


def test_offer_sent_created_needs_confirmation_moves_to_required():
    before = delivery(DeliveryStatus.OFFER_SENT)

    decision = plan_read_evidence_transition(
        before,
        steam_result(before, SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION),
    )

    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == "trade_offer_confirmation_required"
    assert decision.target is not None
    assert (
        decision.target.delivery_status
        is DeliveryStatus.OFFER_CONFIRMATION_REQUIRED
    )


@pytest.mark.parametrize(
    "lifecycle",
    [SteamTradeOfferLifecycle.ACTIVE, SteamTradeOfferLifecycle.ACCEPTED],
)
def test_offer_sent_active_or_accepted_keeps_historical_direct_path(lifecycle):
    before = delivery(DeliveryStatus.OFFER_SENT)

    decision = plan_read_evidence_transition(before, steam_result(before, lifecycle))

    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED


@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
    ],
)
def test_confirmation_state9_remains_safe_wait_without_global_purchase_block(status):
    before = delivery(status)

    decision = plan_read_evidence_transition(
        before,
        steam_result(before, SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION),
    )

    assert decision.target is None
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == "trade_offer_confirmation_still_required"
    assert result_blocks_next_purchase(decision.result) is False


@pytest.mark.parametrize(
    ("status", "lifecycle"),
    [
        (DeliveryStatus.OFFER_CONFIRMATION_REQUIRED, SteamTradeOfferLifecycle.ACTIVE),
        (DeliveryStatus.OFFER_CONFIRMATION_REQUIRED, SteamTradeOfferLifecycle.ACCEPTED),
        (DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED, SteamTradeOfferLifecycle.ACTIVE),
        (DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED, SteamTradeOfferLifecycle.ACCEPTED),
    ],
)
def test_confirmation_states_recover_only_from_exact_active_or_accepted(
    status, lifecycle
):
    before = delivery(status)

    decision = plan_read_evidence_transition(before, steam_result(before, lifecycle))

    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert decision.target.steam_tradeoffer_id == OFFER_ID


def test_confirmation_result_unknown_state9_waits_and_never_falls_back_to_send():
    before = delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )

    decision = plan_read_evidence_transition(
        before,
        steam_result(before, SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION),
    )

    assert decision.target is None
    assert decision.result is AutoOfferResult.WAITING
    assert (
        decision.detail
        == "confirmation_result_unknown_still_requires_confirmation"
    )


@pytest.mark.parametrize(
    "lifecycle",
    [SteamTradeOfferLifecycle.ACTIVE, SteamTradeOfferLifecycle.ACCEPTED],
)
def test_confirmation_result_unknown_recovers_and_clears_write_error(lifecycle):
    before = delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        delivery_error="write_result_unknown",
    )

    decision = plan_read_evidence_transition(before, steam_result(before, lifecycle))

    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert decision.target.delivery_error is None
    assert decision.target.steam_tradeoffer_id == OFFER_ID


def test_wrong_direction_confirmation_evidence_fails_closed():
    before = delivery(DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED)

    decision = plan_read_evidence_transition(
        before,
        steam_result(
            before,
            SteamTradeOfferLifecycle.ACTIVE,
            is_our_offer=False,
        ),
    )

    assert decision.target is None
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.detail == "trade_offer_direction_mismatch"


@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.RESULT_UNKNOWN,
    ],
)
def test_coordinator_routes_confirmation_recovery_to_exact_steam_read(status):
    before = delivery(
        status,
        delivery_error=(
            "write_result_unknown"
            if status is DeliveryStatus.RESULT_UNKNOWN
            else None
        ),
    )
    store = MemoryStore(before)
    adapter = RecordingAdapter(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            detail="still_needs_confirmation",
            evidence=steam_evidence(
                SteamTradeOfferLifecycle.CREATED_NEEDS_CONFIRMATION
            ),
        ),
    )
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.READ_STEAM_TRADE_OFFER: adapter},
        timeout_seconds=5.0,
        clock=lambda: (_ for _ in ()).throw(
            AssertionError("confirmation read recovery must not consume clock")
        ),
    )

    result = coordinator.step(before)

    assert len(adapter.calls) == 1
    assert adapter.calls[0].capability is PlatformCapability.READ_STEAM_TRADE_OFFER
    assert adapter.calls[0].steam_tradeoffer_id == OFFER_ID
    assert result.persisted is False
    assert result.after == before
    assert store.advance_calls == []


def test_coordinator_send_unknown_still_uses_buff_read_without_tradeoffer_id():
    before = delivery(
        DeliveryStatus.RESULT_UNKNOWN,
        steam_tradeoffer_id=None,
        offer_sent_at=None,
        delivery_error="write_result_unknown",
    )
    store = MemoryStore(before)
    adapter = RecordingAdapter(
        PlatformCapability.READ_OFFER_STATE,
        lambda request: PlatformResult(
            request=request,
            status=PlatformResultStatus.RESULT_UNKNOWN,
            detail="order_not_proven",
        ),
    )
    coordinator = DeliveryCoordinator(
        store,
        {PlatformCapability.READ_OFFER_STATE: adapter},
        timeout_seconds=5.0,
    )

    result = coordinator.step(before)

    assert len(adapter.calls) == 1
    assert adapter.calls[0].capability is PlatformCapability.READ_OFFER_STATE
    assert adapter.calls[0].steam_tradeoffer_id is None
    assert result.persisted is False
    assert result.after == before


def test_coordinator_still_rejects_confirm_offer_registry_even_when_writes_allowed():
    current = delivery(DeliveryStatus.OFFER_CONFIRMATION_REQUIRED)
    store = MemoryStore(current)
    adapter = RecordingAdapter(
        PlatformCapability.CONFIRM_OFFER,
        lambda _request: (_ for _ in ()).throw(
            AssertionError("CONFIRM_OFFER must never execute")
        ),
    )

    with pytest.raises(ReadOnlyCoordinatorError, match="adapter_capability_mismatch"):
        DeliveryCoordinator(
            store,
            {PlatformCapability.CONFIRM_OFFER: adapter},
            timeout_seconds=5.0,
            allow_writes=True,
        )

    assert adapter.calls == []
