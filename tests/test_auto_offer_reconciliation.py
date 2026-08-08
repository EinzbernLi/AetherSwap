from dataclasses import FrozenInstanceError, replace

import pytest

from app.auto_offer.adapters import (
    DeliveryDirectionEvidence,
    InventoryStateEvidence,
    OfferStateEvidence,
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
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from app.auto_offer.reconciliation import (
    ReconciliationDecision,
    plan_read_evidence_transition,
)
from app.auto_offer.store import StoredDelivery


IDENTITY = {
    "purchase_id": "purchase-1",
    "buff_order_id": "buff-order-1",
    "account_id": "account-1",
    "recipient_steam_id": "steam-1",
}


def snapshot(
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


def delivery(value=None, revision=1):
    return StoredDelivery(snapshot=value or snapshot(), revision=revision)


def request_for(item, capability, **changes):
    values = {
        "purchase_id": item.snapshot.purchase_id,
        "buff_order_id": item.snapshot.buff_order_id,
        "account_id": item.snapshot.account_id,
        "recipient_steam_id": item.snapshot.recipient_steam_id,
        "revision": item.revision,
        "capability": capability,
        "timeout_seconds": 5.0,
    }
    values.update(changes)
    return PlatformRequest(**values)


def result_for(item, capability, evidence, *, detail=None, **request_changes):
    request = request_for(item, capability, **request_changes)
    return PlatformResult(request, PlatformResultStatus.SUCCESS, detail, evidence)


def non_success(item, status, detail=None):
    return PlatformResult(
        request_for(item, PlatformCapability.READ_DELIVERY_DIRECTION),
        status,
        detail,
    )


def steam_offer_evidence(
    *,
    is_our_offer,
    lifecycle,
    steam_tradeoffer_id="offer-1",
    items_to_give=(),
    items_to_receive=None,
):
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id=steam_tradeoffer_id,
        account_steam_id="steam-1",
        counterparty_steam_id="counterparty-1",
        is_our_offer=is_our_offer,
        lifecycle=lifecycle,
        items_to_give=items_to_give,
        items_to_receive=items_to_receive
        or (TradeOfferItemEvidence(730, "2", "offer-asset-1", 1),),
    )


def test_decision_is_frozen_and_preserves_original_delivery():
    item = delivery()
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
    )
    assert isinstance(decision, ReconciliationDecision)
    with pytest.raises(FrozenInstanceError):
        decision.retryable = False
    assert decision.delivery is item
    assert item.snapshot.delivery_status is DeliveryStatus.PENDING_DIRECTION
    assert item.revision == 1


def test_invalid_delivery_type_and_revision_fail_closed():
    evidence = DeliveryDirectionEvidence()
    with pytest.raises(DeliveryContractError):
        plan_read_evidence_transition(object(), object())
    with pytest.raises(DeliveryContractError):
        plan_read_evidence_transition(
            delivery(revision=True),
            result_for(delivery(), PlatformCapability.READ_DELIVERY_DIRECTION, evidence),
        )


@pytest.mark.parametrize(
    "field",
    ["purchase_id", "buff_order_id", "account_id", "recipient_steam_id"],
)
def test_each_identity_mismatch_blocks_before_evidence_interpretation(field):
    item = delivery()
    result = result_for(
        item,
        PlatformCapability.READ_DELIVERY_DIRECTION,
        DeliveryDirectionEvidence(),
        **{field: "different"},
    )
    decision = plan_read_evidence_transition(item, result)
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.retryable is False
    assert decision.target is None
    assert decision.detail == "identity_mismatch"


def test_revision_mismatch_blocks_exactly():
    item = delivery(revision=2)
    result = result_for(
        item,
        PlatformCapability.READ_DELIVERY_DIRECTION,
        DeliveryDirectionEvidence(),
        revision=1,
    )
    decision = plan_read_evidence_transition(item, result)
    assert (decision.result, decision.retryable, decision.target, decision.detail) == (
        AutoOfferResult.BLOCKED,
        False,
        None,
        "identity_mismatch",
    )


@pytest.mark.parametrize(
    "status,detail,expected_result,expected_retryable,expected_detail",
    [
        (
            PlatformResultStatus.RESULT_UNKNOWN,
            "anything",
            AutoOfferResult.WAITING,
            True,
            "read_result_unknown",
        ),
        (
            PlatformResultStatus.TIMEOUT,
            "anything",
            AutoOfferResult.WAITING,
            True,
            "read_timeout",
        ),
        (
            PlatformResultStatus.FAILURE,
            "network_failure",
            AutoOfferResult.BLOCKED,
            False,
            "read_failure",
        ),
        (
            PlatformResultStatus.MALFORMED,
            "malformed_payload",
            AutoOfferResult.BLOCKED,
            False,
            "malformed_result",
        ),
        (
            PlatformResultStatus.UNSUPPORTED,
            "unsupported_capability",
            AutoOfferResult.BLOCKED,
            False,
            "unsupported_capability",
        ),
    ],
)
def test_non_success_results_are_normalized_fail_closed(
    status, detail, expected_result, expected_retryable, expected_detail
):
    item = delivery()
    decision = plan_read_evidence_transition(item, non_success(item, status, detail))
    assert decision.result is expected_result
    assert decision.retryable is expected_retryable
    assert decision.target is None
    assert decision.detail == expected_detail


def test_platform_identity_mismatch_detail_remains_blocked():
    item = delivery()
    decision = plan_read_evidence_transition(
        item,
        non_success(item, PlatformResultStatus.FAILURE, "identity_mismatch"),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.retryable is False
    assert decision.detail == "identity_mismatch"


def test_pending_direction_proposes_only_seller_awaiting_offer():
    item = delivery()
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == "seller_direction_proven"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.AWAITING_OFFER
    assert decision.target.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
    assert decision.target.steam_tradeoffer_id is None
    assert decision.target.assetid is None
    assert decision.target.received_at is None
    validate_delivery_snapshot(decision.target)
    validate_delivery_transition(item.snapshot, decision.target)


def test_pending_direction_wrong_capability_cannot_advance():
    item = delivery()
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-1")),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.detail == "evidence_not_allowed"


def test_awaiting_seller_offer_proposes_exact_offer_received():
    item = delivery(snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.SELLER_SENDS_OFFER))
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-42")),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == "seller_offer_proven"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_RECEIVED
    assert decision.target.delivery_mode is DeliveryMode.SELLER_SENDS_OFFER
    assert decision.target.steam_tradeoffer_id == "offer-42"
    assert decision.target.offer_attempted_at is None
    assert decision.target.offer_sent_at is None
    assert decision.target.received_at is None
    validate_delivery_snapshot(decision.target)
    validate_delivery_transition(item.snapshot, decision.target)


def test_awaiting_seller_offer_wrong_capability_cannot_advance():
    item = delivery(snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.SELLER_SENDS_OFFER))
    decision = plan_read_evidence_transition(
        item,
        result_for(
            item,
            PlatformCapability.READ_DELIVERY_DIRECTION,
            DeliveryDirectionEvidence(),
        ),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None


def test_buyer_mode_never_plans_first_send_or_synthetic_fields():
    item = delivery(snapshot(DeliveryStatus.AWAITING_OFFER, DeliveryMode.BUYER_SENDS_OFFER))
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-1")),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.retryable is False
    assert decision.target is None
    assert decision.detail == "write_capability_required"


def test_result_unknown_delivery_never_plans_resend():
    item = delivery(
        snapshot(
            DeliveryStatus.RESULT_UNKNOWN,
            DeliveryMode.BUYER_SENDS_OFFER,
            delivery_error="write_result_unknown",
        )
    )
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.target is None
    assert decision.detail == "result_unknown_recovery_not_planned"


@pytest.mark.parametrize("assetids", [(), ("asset-1",), ("asset-1", "asset-2")])
def test_inventory_evidence_never_proves_purchase_receipt(assetids):
    item = delivery(
        snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    evidence = InventoryStateEvidence(assetids, len(assetids))
    before = item
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_INVENTORY_STATE, evidence),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.target is None
    assert decision.detail == "purchase_asset_not_proven"
    assert decision.delivery is before
    assert item.snapshot.delivery_status is DeliveryStatus.AWAITING_INVENTORY
    assert item.snapshot.assetid is None
    assert item.snapshot.received_at is None
    assert item.snapshot.pending_receipt is True


def test_awaiting_inventory_wrong_success_cannot_advance():
    item = delivery(
        snapshot(
            DeliveryStatus.AWAITING_INVENTORY,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-1")),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None


@pytest.mark.parametrize(
    "status,mode,kwargs,is_our_offer,lifecycle,detail",
    [
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACTIVE,
            "trade_offer_confirmed_active",
        ),
        (
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {},
            False,
            SteamTradeOfferLifecycle.ACCEPTED,
            "trade_offer_confirmed_accepted",
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACTIVE,
            "trade_offer_confirmed_active",
        ),
        (
            DeliveryStatus.OFFER_SENT,
            DeliveryMode.BUYER_SENDS_OFFER,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            True,
            SteamTradeOfferLifecycle.ACCEPTED,
            "trade_offer_confirmed_accepted",
        ),
    ],
)
def test_trade_offer_proof_advances_only_to_offer_confirmed(
    status, mode, kwargs, is_our_offer, lifecycle, detail
):
    item = delivery(snapshot(status, mode, steam_tradeoffer_id="offer-1", **kwargs))
    decision = plan_read_evidence_transition(
        item,
        result_for(
            item,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_offer_evidence(is_our_offer=is_our_offer, lifecycle=lifecycle),
            steam_tradeoffer_id="offer-1",
        ),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == detail
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert decision.target.steam_tradeoffer_id == "offer-1"
    assert decision.target.assetid is None
    assert decision.target.received_at is None
    assert decision.target.pending_receipt is True
    assert decision.target.offer_attempted_at == item.snapshot.offer_attempted_at
    assert decision.target.offer_sent_at == item.snapshot.offer_sent_at


@pytest.mark.parametrize(
    "mode,expected_is_our_offer,kwargs",
    [
        (DeliveryMode.SELLER_SENDS_OFFER, False, {}),
        (
            DeliveryMode.BUYER_SENDS_OFFER,
            True,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
        ),
    ],
)
@pytest.mark.parametrize(
    "lifecycle,expected_status,detail",
    [
        (SteamTradeOfferLifecycle.ACTIVE, None, "trade_offer_not_accepted"),
        (
            SteamTradeOfferLifecycle.ACCEPTED,
            DeliveryStatus.AWAITING_INVENTORY,
            "trade_offer_accepted",
        ),
    ],
)
def test_offer_confirmed_requires_acceptance_before_inventory(
    mode, expected_is_our_offer, kwargs, lifecycle, expected_status, detail
):
    item = delivery(
        snapshot(
            DeliveryStatus.OFFER_CONFIRMED,
            mode,
            steam_tradeoffer_id="offer-1",
            **kwargs,
        )
    )
    decision = plan_read_evidence_transition(
        item,
        result_for(
            item,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_offer_evidence(
                is_our_offer=expected_is_our_offer,
                lifecycle=lifecycle,
                items_to_receive=(
                    TradeOfferItemEvidence(730, "2", "offer-asset-1", 1),
                    TradeOfferItemEvidence(730, "2", "offer-asset-2", 1),
                ),
            ),
            steam_tradeoffer_id="offer-1",
        ),
    )
    assert decision.result is AutoOfferResult.WAITING
    assert decision.retryable is True
    assert decision.detail == detail
    if expected_status is None:
        assert decision.target is None
    else:
        assert decision.target is not None
        assert decision.target.delivery_status is expected_status
        assert decision.target.assetid is None
        assert decision.target.received_at is None
        assert decision.target.pending_receipt is True


@pytest.mark.parametrize(
    "mode,status,kwargs,is_our_offer,items_to_give,expected_detail",
    [
        (
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryStatus.OFFER_RECEIVED,
            {},
            True,
            (),
            "trade_offer_direction_mismatch",
        ),
        (
            DeliveryMode.BUYER_SENDS_OFFER,
            DeliveryStatus.OFFER_SENT,
            {"offer_attempted_at": 1.0, "offer_sent_at": 2.0},
            False,
            (),
            "trade_offer_direction_mismatch",
        ),
        (
            DeliveryMode.SELLER_SENDS_OFFER,
            DeliveryStatus.OFFER_RECEIVED,
            {},
            False,
            (TradeOfferItemEvidence(730, "2", "give-asset-1", 1),),
            "trade_offer_outgoing_items_present",
        ),
    ],
)
def test_trade_offer_direction_and_outgoing_items_fail_closed(
    mode, status, kwargs, is_our_offer, items_to_give, expected_detail
):
    item = delivery(snapshot(status, mode, steam_tradeoffer_id="offer-1", **kwargs))
    decision = plan_read_evidence_transition(
        item,
        result_for(
            item,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_offer_evidence(
                is_our_offer=is_our_offer,
                lifecycle=SteamTradeOfferLifecycle.ACTIVE,
                items_to_give=items_to_give,
            ),
            steam_tradeoffer_id="offer-1",
        ),
    )
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.retryable is False
    assert decision.target is None
    assert decision.detail == expected_detail


def test_trade_offer_requires_exact_snapshot_request_and_evidence_binding():
    item = delivery(
        snapshot(
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    mismatched = plan_read_evidence_transition(
        item,
        result_for(
            item,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_offer_evidence(
                is_our_offer=False,
                lifecycle=SteamTradeOfferLifecycle.ACTIVE,
                steam_tradeoffer_id="offer-2",
            ),
            steam_tradeoffer_id="offer-2",
        ),
    )
    wrong_capability = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-1")),
    )
    assert mismatched.result is AutoOfferResult.BLOCKED
    assert mismatched.target is None
    assert mismatched.retryable is False
    assert mismatched.detail == "identity_mismatch"
    assert wrong_capability.result is AutoOfferResult.BLOCKED
    assert wrong_capability.target is None


@pytest.mark.parametrize(
    "status",
    [PlatformResultStatus.RESULT_UNKNOWN, PlatformResultStatus.TIMEOUT],
)
def test_trade_offer_request_id_mismatch_blocks_before_non_success_mapping(status):
    item = delivery(
        snapshot(
            DeliveryStatus.OFFER_RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            steam_tradeoffer_id="offer-1",
        )
    )
    result = PlatformResult(
        request_for(
            item,
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            steam_tradeoffer_id="offer-2",
        ),
        status,
        "read_not_current",
    )
    decision = plan_read_evidence_transition(item, result)
    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None
    assert decision.retryable is False
    assert decision.detail == "identity_mismatch"


@pytest.mark.parametrize(
    "status,mode,kwargs,expected_result",
    [
        (
            DeliveryStatus.RECEIVED,
            DeliveryMode.SELLER_SENDS_OFFER,
            {"steam_tradeoffer_id": "offer-1", "received_at": 3.0, "pending_receipt": False, "assetid": "asset-1"},
            AutoOfferResult.COMPLETE,
        ),
        (DeliveryStatus.BLOCKED, None, {}, AutoOfferResult.BLOCKED),
        (DeliveryStatus.CANCELLED, None, {}, AutoOfferResult.COMPLETE),
        (DeliveryStatus.REFUNDED, None, {}, AutoOfferResult.COMPLETE),
    ],
)
def test_terminal_states_never_produce_a_target(status, mode, kwargs, expected_result):
    item = delivery(snapshot(status, mode, **kwargs))
    decision = plan_read_evidence_transition(
        item,
        result_for(item, PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
    )
    assert decision.result is expected_result
    assert decision.retryable is False
    assert decision.target is None


def test_terminal_result_semantics_match_the_executor_contract():
    expected = {
        DeliveryStatus.RECEIVED: AutoOfferResult.COMPLETE,
        DeliveryStatus.CANCELLED: AutoOfferResult.COMPLETE,
        DeliveryStatus.REFUNDED: AutoOfferResult.COMPLETE,
        DeliveryStatus.BLOCKED: AutoOfferResult.BLOCKED,
    }
    for status, expected_result in expected.items():
        kwargs = {}
        if status is DeliveryStatus.RECEIVED:
            kwargs = {
                "steam_tradeoffer_id": "offer-1",
                "received_at": 3.0,
                "pending_receipt": False,
                "assetid": "asset-1",
            }
        item = delivery(snapshot(status, DeliveryMode.SELLER_SENDS_OFFER if status is DeliveryStatus.RECEIVED else None, **kwargs))
        decision = plan_read_evidence_transition(
            item,
            result_for(item, PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
        )
        assert decision.result is expected_result
        assert decision.retryable is False
        assert decision.target is None


def test_no_store_or_runtime_side_effect_surface_in_planner():
    path = __import__("pathlib").Path(__file__).parents[1] / "app" / "auto_offer" / "reconciliation.py"
    text = path.read_text(encoding="utf-8")
    forbidden = (
        "AutoOfferStore",
        "sqlite",
        "Pipeline",
        "Purchase Flow",
        "requests",
        "httpx",
        "aiohttp",
        "sleep(",
        "Thread",
        "SEND_OFFER",
    )
    for term in forbidden:
        assert term not in text
