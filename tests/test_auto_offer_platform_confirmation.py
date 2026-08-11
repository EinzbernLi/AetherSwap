import pytest

from app.auto_offer.adapters import (
    ConfirmOfferEvidence,
    DEFAULT_PLATFORM_CAPABILITIES,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
)
from app.auto_offer.coordinator import DeliveryCoordinator, ReadOnlyCoordinatorError
from app.auto_offer.platform_confirmation import SteamTradeOfferConfirmationAdapter
from app.auto_offer.steam_confirmation_transport import (
    SteamConfirmationTransportAuthError,
    SteamConfirmationTransportError,
    SteamConfirmationWriteResultUnknown,
)


ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000001"
OFFER_ID = "9876543210"


class FakeTransport:
    def __init__(self, outcome=None, *, bound_account=STEAM_ID):
        self.bound_account_steam_id = bound_account
        self.outcome = outcome
        self.calls = []

    def confirm(self, steam_tradeoffer_id, *, timeout_seconds):
        self.calls.append((steam_tradeoffer_id, timeout_seconds))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is None:
            return {
                "steam_tradeoffer_id": steam_tradeoffer_id,
                "account_steam_id": self.bound_account_steam_id,
            }
        return self.outcome


def _request(
    *,
    capability=PlatformCapability.CONFIRM_OFFER,
    account_id=ACCOUNT_ID,
    recipient_steam_id=STEAM_ID,
    steam_tradeoffer_id=OFFER_ID,
):
    return PlatformRequest(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id=account_id,
        recipient_steam_id=recipient_steam_id,
        revision=5,
        capability=capability,
        timeout_seconds=15.0,
        steam_tradeoffer_id=steam_tradeoffer_id,
    )


def _adapter(transport=None):
    return SteamTradeOfferConfirmationAdapter(
        FakeTransport() if transport is None else transport,
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
    )


def test_confirm_offer_request_requires_exact_tradeoffer_id():
    with pytest.raises(PlatformAdapterProtocolError):
        _request(steam_tradeoffer_id=None)


def test_confirm_offer_success_requires_exact_typed_identity_evidence():
    transport = FakeTransport()
    result = _adapter(transport).execute(_request())

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.detail == "trade_offer_mobile_confirmed"
    assert result.evidence == ConfirmOfferEvidence(
        steam_tradeoffer_id=OFFER_ID,
        account_steam_id=STEAM_ID,
    )
    assert transport.calls == [(OFFER_ID, 15.0)]


def test_constructor_rejects_transport_bound_to_other_steam_account():
    with pytest.raises(
        PlatformAdapterProtocolError,
        match="transport Steam identity does not match recipient_steam_id",
    ):
        _adapter(FakeTransport(bound_account="76561198000000002"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "other-account"),
        ("recipient_steam_id", "76561198000000002"),
    ],
)
def test_request_identity_mismatch_blocks_before_transport(field, value):
    transport = FakeTransport()
    request = _request(**{field: value})
    result = _adapter(transport).execute(request)

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert transport.calls == []


def test_unsupported_capability_does_not_call_transport():
    transport = FakeTransport()
    request = _request(
        capability=PlatformCapability.READ_STEAM_TRADE_OFFER,
        steam_tradeoffer_id=OFFER_ID,
    )
    result = _adapter(transport).execute(request)

    assert result.status is PlatformResultStatus.UNSUPPORTED
    assert transport.calls == []


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (
            SteamConfirmationWriteResultUnknown("secret-data"),
            PlatformResultStatus.RESULT_UNKNOWN,
            "confirmation_result_unknown",
        ),
        (
            PlatformAdapterTimeoutError("secret-data"),
            PlatformResultStatus.TIMEOUT,
            "confirmation_read_timeout",
        ),
        (
            SteamConfirmationTransportAuthError("secret-data"),
            PlatformResultStatus.FAILURE,
            "confirmation_auth_failed",
        ),
        (
            SteamConfirmationTransportError("secret-data"),
            PlatformResultStatus.FAILURE,
            "confirmation_preflight_failed",
        ),
        (
            PlatformAdapterProtocolError("secret-data"),
            PlatformResultStatus.MALFORMED,
            "confirmation_protocol_error",
        ),
        (
            RuntimeError("secret-data"),
            PlatformResultStatus.RESULT_UNKNOWN,
            "confirmation_result_unknown",
        ),
    ],
)
def test_transport_failures_are_sanitized_without_success_evidence(error, status, detail):
    transport = FakeTransport(error)
    result = _adapter(transport).execute(_request())

    assert result.status is status
    assert result.detail == detail
    assert result.evidence is None
    assert "secret-data" not in repr(result)


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        "not-a-dict",
        {"steam_tradeoffer_id": "111", "account_steam_id": STEAM_ID},
        {"steam_tradeoffer_id": OFFER_ID, "account_steam_id": "76561198000000002"},
    ],
)
def test_unproven_or_mismatched_success_shape_is_malformed(outcome):
    transport = FakeTransport(outcome)
    if outcome is None:
        # None is the FakeTransport sentinel for canonical success.
        return
    result = _adapter(transport).execute(_request())

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.evidence is None


def test_platform_result_contract_rejects_confirmation_evidence_for_other_offer():
    request = _request()
    with pytest.raises(PlatformAdapterProtocolError):
        from app.auto_offer.adapters import PlatformResult
        PlatformResult(
            request=request,
            status=PlatformResultStatus.SUCCESS,
            evidence=ConfirmOfferEvidence(
                steam_tradeoffer_id="111",
                account_steam_id=STEAM_ID,
            ),
        )


def test_confirm_offer_is_not_a_default_platform_capability():
    assert PlatformCapability.CONFIRM_OFFER not in DEFAULT_PLATFORM_CAPABILITIES


def test_task029_does_not_wire_confirmation_into_delivery_coordinator():
    class Store:
        def get_by_purchase_id(self, _purchase_id):
            return None

        def advance(self, _current, _target):
            raise AssertionError("not expected")

    with pytest.raises(ReadOnlyCoordinatorError, match="adapter_capability_mismatch"):
        DeliveryCoordinator(
            Store(),
            {PlatformCapability.CONFIRM_OFFER: _adapter()},
            timeout_seconds=15.0,
            allow_writes=True,
        )
