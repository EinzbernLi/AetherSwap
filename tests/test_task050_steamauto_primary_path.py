import pytest

from app.auto_offer.adapters import (
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
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
    validate_delivery_transition,
)
from app.auto_offer.platform_readonly import BuffReadOnlyAdapter
from app.auto_offer.reconciliation import plan_read_evidence_transition
from app.auto_offer.store import StoredDelivery


RECIPIENT = "76561198000000001"
COUNTERPARTY = "76561198000000002"


class BuffStub:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_steam_trades(self):
        self.calls += 1
        return self.payload


def request(capability=PlatformCapability.READ_OFFER_STATE, **changes):
    values = {
        "purchase_id": "purchase-1",
        "buff_order_id": "order-1",
        "account_id": "account-1",
        "recipient_steam_id": RECIPIENT,
        "revision": 1,
        "capability": capability,
        "timeout_seconds": 5.0,
    }
    values.update(changes)
    return PlatformRequest(**values)


def buyer_snapshot(
    status,
    *,
    steam_tradeoffer_id=None,
    offer_attempted_at=10.0,
    offer_sent_at=None,
    delivery_error=None,
    counterparty_steam_id=None,
):
    if (
        status is DeliveryStatus.OFFER_SENT
        and steam_tradeoffer_id is not None
        and counterparty_steam_id is None
        and delivery_error is None
    ):
        delivery_error = "offer_identity_pending"
    value = DeliverySnapshot(
        purchase_id="purchase-1",
        buff_order_id="order-1",
        account_id="account-1",
        recipient_steam_id=RECIPIENT,
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=status,
        steam_tradeoffer_id=steam_tradeoffer_id,
        offer_attempted_at=offer_attempted_at,
        offer_sent_at=offer_sent_at,
        received_at=None,
        delivery_error=delivery_error,
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id=counterparty_steam_id,
    )
    validate_delivery_snapshot(value)
    return value


def steam_offer_evidence(*, is_our_offer=True, items_to_give=()):
    return SteamTradeOfferEvidence(
        steam_tradeoffer_id="offer-1",
        account_steam_id=RECIPIENT,
        counterparty_steam_id=COUNTERPARTY,
        is_our_offer=is_our_offer,
        lifecycle=SteamTradeOfferLifecycle.ACTIVE,
        items_to_give=items_to_give,
        items_to_receive=(
            TradeOfferItemEvidence(730, "2", "asset-1", 1),
        ),
    )


def test_offer_state_evidence_allows_tradeoffer_only_binding():
    evidence = OfferStateEvidence("offer-1")
    assert evidence.steam_tradeoffer_id == "offer-1"
    assert evidence.counterparty_steam_id is None


def test_realtime_buff_id_binds_exact_tradeoffer_without_seller_fields():
    client = BuffStub([{"id": "order-1", "tradeofferid": "offer-1"}])

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert client.calls == 1
    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "offer_bound_realtime"
    assert result.evidence == OfferStateEvidence("offer-1")


def test_realtime_buff_explicit_recipient_mismatch_fails_closed():
    client = BuffStub(
        [
            {
                "id": "order-1",
                "tradeofferid": "offer-1",
                "buyer_steamid": COUNTERPARTY,
            }
        ]
    )

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"


def test_realtime_buff_zero_exact_match_is_safe_wait():
    client = BuffStub([{"id": "other-order", "tradeofferid": "offer-2"}])

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"


def test_realtime_buff_two_exact_matches_fail_closed():
    client = BuffStub(
        [
            {"id": "order-1", "tradeofferid": "offer-1"},
            {"id": "order-1", "tradeofferid": "offer-1"},
        ]
    )

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "ambiguous_order"


def test_realtime_buff_conflicting_order_aliases_fail_closed():
    client = BuffStub(
        [
            {
                "id": "order-1",
                "buff_order_id": "different-order",
                "tradeofferid": "offer-1",
            }
        ]
    )

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"


def test_realtime_buff_conflicting_offer_aliases_fail_closed():
    client = BuffStub(
        [
            {
                "id": "order-1",
                "tradeofferid": "offer-1",
                "trade_offer_id": "offer-2",
            }
        ]
    )

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "malformed_payload"


def test_realtime_buff_missing_tradeoffer_is_safe_wait():
    client = BuffStub([{"id": "order-1"}])

    result = BuffReadOnlyAdapter(client, account_id="account-1").execute(request())

    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "order_not_proven"


@pytest.mark.parametrize(
    ("status", "delivery_error"),
    [
        (DeliveryStatus.OFFER_ATTEMPTED, None),
        (DeliveryStatus.RESULT_UNKNOWN, "write_result_unknown"),
    ],
)
def test_buff_realtime_binding_recovers_buyer_without_counterparty(
    status, delivery_error
):
    current = buyer_snapshot(status, delivery_error=delivery_error)
    stored = StoredDelivery(snapshot=current, revision=1)
    read_request = request()
    read_result = PlatformResult(
        request=read_request,
        status=PlatformResultStatus.SUCCESS,
        detail="offer_bound_realtime",
        evidence=OfferStateEvidence("offer-1"),
    )

    decision = plan_read_evidence_transition(
        stored,
        read_result,
        observed_at=11.0,
    )

    assert decision.result is AutoOfferResult.WAITING
    assert decision.detail == "buyer_offer_recovered"
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_SENT
    assert decision.target.steam_tradeoffer_id == "offer-1"
    assert decision.target.counterparty_steam_id is None
    assert decision.target.delivery_error == "offer_identity_pending"
    assert decision.target.offer_sent_at == 11.0
    validate_delivery_transition(current, decision.target)


def test_first_exact_steam_read_binds_counterparty_and_advances_buyer():
    current = buyer_snapshot(
        DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_sent_at=11.0,
    )
    stored = StoredDelivery(snapshot=current, revision=2)
    read_request = request(
        PlatformCapability.READ_STEAM_TRADE_OFFER,
        revision=2,
        steam_tradeoffer_id="offer-1",
    )
    read_result = PlatformResult(
        request=read_request,
        status=PlatformResultStatus.SUCCESS,
        detail="active",
        evidence=steam_offer_evidence(),
    )

    decision = plan_read_evidence_transition(stored, read_result)

    assert decision.result is AutoOfferResult.WAITING
    assert decision.target is not None
    assert decision.target.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert decision.target.steam_tradeoffer_id == "offer-1"
    assert decision.target.counterparty_steam_id == COUNTERPARTY
    assert decision.target.delivery_error is None
    validate_delivery_transition(current, decision.target)


def test_historical_unmarked_offer_sent_cannot_adopt_counterparty():
    current = DeliverySnapshot(
        purchase_id="purchase-1",
        buff_order_id="order-1",
        account_id="account-1",
        recipient_steam_id=RECIPIENT,
        delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
        delivery_status=DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_attempted_at=10.0,
        offer_sent_at=11.0,
        received_at=None,
        delivery_error=None,
        pending_receipt=True,
        assetid=None,
        counterparty_steam_id=None,
    )
    stored = StoredDelivery(snapshot=current, revision=2)
    read_result = PlatformResult(
        request=request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            revision=2,
            steam_tradeoffer_id="offer-1",
        ),
        status=PlatformResultStatus.SUCCESS,
        detail="active",
        evidence=steam_offer_evidence(),
    )

    decision = plan_read_evidence_transition(stored, read_result)

    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.target is None


def test_exact_steam_wrong_direction_blocks_after_buff_binding():
    current = buyer_snapshot(
        DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_sent_at=11.0,
    )
    stored = StoredDelivery(snapshot=current, revision=2)
    read_result = PlatformResult(
        request=request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            revision=2,
            steam_tradeoffer_id="offer-1",
        ),
        status=PlatformResultStatus.SUCCESS,
        detail="active",
        evidence=steam_offer_evidence(is_our_offer=False),
    )

    decision = plan_read_evidence_transition(stored, read_result)

    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.detail == "trade_offer_direction_mismatch"
    assert decision.target is None


def test_exact_steam_outgoing_items_block_after_buff_binding():
    current = buyer_snapshot(
        DeliveryStatus.OFFER_SENT,
        steam_tradeoffer_id="offer-1",
        offer_sent_at=11.0,
    )
    stored = StoredDelivery(snapshot=current, revision=2)
    outgoing = (TradeOfferItemEvidence(730, "2", "unexpected-outgoing", 1),)
    read_result = PlatformResult(
        request=request(
            PlatformCapability.READ_STEAM_TRADE_OFFER,
            revision=2,
            steam_tradeoffer_id="offer-1",
        ),
        status=PlatformResultStatus.SUCCESS,
        detail="active",
        evidence=steam_offer_evidence(items_to_give=outgoing),
    )

    decision = plan_read_evidence_transition(stored, read_result)

    assert decision.result is AutoOfferResult.BLOCKED
    assert decision.detail == "trade_offer_outgoing_items_present"
    assert decision.target is None
