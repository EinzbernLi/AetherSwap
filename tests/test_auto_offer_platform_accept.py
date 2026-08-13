from __future__ import annotations

from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    AcceptOfferEvidence,
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
)
from app.auto_offer.platform_accept import (
    ACCEPT_OFFER_CAPABILITIES,
    AcceptOfferPreflightError,
    AcceptOfferWriteResultUnknown,
    SteamIncomingOfferAcceptAdapter,
)


ACCOUNT_ID = "account-1"
OUR_STEAM_ID = "76561198000000001"
SELLER_STEAM_ID = "76561198000000002"
OFFER_ID = "1234567890"


def request(**changes):
    value = PlatformRequest(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id=ACCOUNT_ID,
        recipient_steam_id=OUR_STEAM_ID,
        revision=7,
        capability=PlatformCapability.ACCEPT_OFFER,
        steam_tradeoffer_id=OFFER_ID,
        timeout_seconds=5.0,
    )
    return replace(value, **changes)


class Transport:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def accept(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def adapter(transport):
    return SteamIncomingOfferAcceptAdapter(
        transport,
        account_id=ACCOUNT_ID,
        recipient_steam_id=OUR_STEAM_ID,
        expected_counterparty_steam_id=SELLER_STEAM_ID,
    )


def success_response(**changes):
    value = {
        "accepted": True,
        "steam_tradeoffer_id": OFFER_ID,
        "account_steam_id": OUR_STEAM_ID,
    }
    value.update(changes)
    return value


def test_capability_is_exact_and_immutable():
    transport = Transport(response=success_response())
    value = adapter(transport)
    assert value.capabilities == ACCEPT_OFFER_CAPABILITIES
    assert value.capabilities == frozenset({PlatformCapability.ACCEPT_OFFER})
    with pytest.raises(AttributeError):
        value.capabilities.add(PlatformCapability.SEND_OFFER)


def test_wrong_capability_or_identity_never_enters_transport():
    transport = Transport(response=success_response())
    value = adapter(transport)

    unsupported = value.execute(
        request(capability=PlatformCapability.READ_STEAM_TRADE_OFFER)
    )
    wrong_account = value.execute(request(account_id="other-account"))
    wrong_recipient = value.execute(
        request(recipient_steam_id="76561198000000003")
    )

    assert unsupported.status is PlatformResultStatus.UNSUPPORTED
    assert wrong_account.status is PlatformResultStatus.FAILURE
    assert wrong_account.detail == "identity_mismatch"
    assert wrong_recipient.status is PlatformResultStatus.FAILURE
    assert wrong_recipient.detail == "identity_mismatch"
    assert transport.calls == []


def test_exact_success_calls_transport_once_and_binds_request_identity():
    transport = Transport(response=success_response())
    value = adapter(transport)
    item = request()

    result = value.execute(item)

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "accept_returned_success"
    assert result.evidence == AcceptOfferEvidence(OFFER_ID, OUR_STEAM_ID)
    assert transport.calls == [
        {
            "steam_tradeoffer_id": OFFER_ID,
            "account_steam_id": OUR_STEAM_ID,
            "counterparty_steam_id": SELLER_STEAM_ID,
            "timeout_seconds": 5.0,
        }
    ]


def test_local_preflight_rejection_is_known_failure_without_success_claim():
    transport = Transport(error=AcceptOfferPreflightError("blocked"))
    result = adapter(transport).execute(request())
    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "write_preflight_failed"
    assert result.evidence is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        AcceptOfferWriteResultUnknown("unknown"),
        TimeoutError("timeout"),
        RuntimeError("arbitrary"),
    ],
)
def test_any_ambiguous_post_exception_is_result_unknown_and_not_retried(error):
    transport = Transport(error=error)
    result = adapter(transport).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "write_result_unknown"
    assert result.evidence is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"accepted": None},
        success_response(steam_tradeoffer_id="other-offer"),
        success_response(account_steam_id="76561198000000003"),
    ],
)
def test_unproven_or_mismatched_success_response_is_result_unknown(response):
    transport = Transport(response=response)
    result = adapter(transport).execute(request())
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "write_result_unknown"
    assert result.evidence is None
    assert len(transport.calls) == 1


def test_explicit_rejection_is_known_failure_and_never_retried():
    transport = Transport(response={"accepted": False})
    result = adapter(transport).execute(request())
    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "accept_rejected"
    assert result.evidence is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "recipient,counterparty",
    [
        (" steam", SELLER_STEAM_ID),
        ("01", SELLER_STEAM_ID),
        (OUR_STEAM_ID, "seller"),
        (OUR_STEAM_ID, OUR_STEAM_ID),
    ],
)
def test_constructor_rejects_noncanonical_or_self_counterparty(
    recipient, counterparty
):
    with pytest.raises(PlatformAdapterProtocolError):
        SteamIncomingOfferAcceptAdapter(
            Transport(response=success_response()),
            account_id=ACCOUNT_ID,
            recipient_steam_id=recipient,
            expected_counterparty_steam_id=counterparty,
        )
