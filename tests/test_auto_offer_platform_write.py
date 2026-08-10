import pytest

from app.auto_offer.adapters import (
    PlatformAdapterProtocolError,
    PlatformCapability,
    PlatformRequest,
    PlatformResultStatus,
)
from app.auto_offer.platform_write import (
    BuffBuyerSendOfferAdapter,
    SEND_OFFER_CAPABILITIES,
)
from buff.buyer_send import BuffBuyerSendError
from buff.request_policy import BuffWriteResultUnknown

ACCOUNT_ID = "account-1"
STEAM_ID = "76561198000000000"
ORDER_ID = "123456789"
SECRET = "sessionid=session-secret; steamLoginSecure=login-secret"


class FakeTransport:
    def __init__(self, outcome=None):
        self.outcome = {"code": "OK", "data": {}} if outcome is None else outcome
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def request(
    *,
    account_id=ACCOUNT_ID,
    recipient_steam_id=STEAM_ID,
    capability=PlatformCapability.SEND_OFFER,
    order_id=ORDER_ID,
):
    return PlatformRequest(
        purchase_id=f"buff:{order_id}",
        buff_order_id=order_id,
        account_id=account_id,
        recipient_steam_id=recipient_steam_id,
        revision=3,
        capability=capability,
        timeout_seconds=6.5,
    )


def make_adapter(transport=None, provider=lambda: SECRET):
    return BuffBuyerSendOfferAdapter(
        transport or FakeTransport(),
        account_id=ACCOUNT_ID,
        recipient_steam_id=STEAM_ID,
        steam_cookie_provider=provider,
    )


def test_adapter_declares_send_offer_only():
    adapter = make_adapter()
    assert adapter.capabilities == SEND_OFFER_CAPABILITIES
    assert adapter.capabilities == frozenset({PlatformCapability.SEND_OFFER})


def test_constructor_rejects_invalid_dependencies_and_identity():
    with pytest.raises(PlatformAdapterProtocolError):
        BuffBuyerSendOfferAdapter(
            object(),
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            steam_cookie_provider=lambda: SECRET,
        )
    with pytest.raises(PlatformAdapterProtocolError):
        BuffBuyerSendOfferAdapter(
            FakeTransport(),
            account_id="",
            recipient_steam_id=STEAM_ID,
            steam_cookie_provider=lambda: SECRET,
        )
    with pytest.raises(PlatformAdapterProtocolError):
        BuffBuyerSendOfferAdapter(
            FakeTransport(),
            account_id=ACCOUNT_ID,
            recipient_steam_id="001",
            steam_cookie_provider=lambda: SECRET,
        )
    with pytest.raises(PlatformAdapterProtocolError):
        BuffBuyerSendOfferAdapter(
            FakeTransport(),
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            steam_cookie_provider=None,
        )


def test_repr_does_not_capture_or_expose_cookie_secret():
    adapter = make_adapter()
    text = repr(adapter)
    assert "session-secret" not in text
    assert "login-secret" not in text
    assert ACCOUNT_ID in text
    assert STEAM_ID in text


def test_wrong_capability_is_unsupported_without_cookie_or_transport_call():
    transport = FakeTransport()
    provider_calls = []

    def provider():
        provider_calls.append(True)
        return SECRET

    adapter = make_adapter(transport, provider)
    result = adapter.execute(
        request(capability=PlatformCapability.READ_OFFER_STATE)
    )

    assert result.status is PlatformResultStatus.UNSUPPORTED
    assert result.detail == "unsupported_capability"
    assert provider_calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": "other"},
        {"recipient_steam_id": "76561198000000001"},
    ],
)
def test_identity_mismatch_blocks_before_cookie_and_transport(kwargs):
    transport = FakeTransport()
    provider_calls = []

    def provider():
        provider_calls.append(True)
        return SECRET

    adapter = make_adapter(transport, provider)
    result = adapter.execute(request(**kwargs))

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "identity_mismatch"
    assert provider_calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "provider",
    [
        lambda: "",
        lambda: None,
        lambda: (_ for _ in ()).throw(RuntimeError("cookie-secret")),
    ],
)
def test_cookie_provider_failure_blocks_before_transport(provider):
    transport = FakeTransport()
    adapter = make_adapter(transport, provider)

    result = adapter.execute(request())

    assert result.status is PlatformResultStatus.FAILURE
    assert result.detail == "credential_unavailable"
    assert result.evidence is None
    assert transport.calls == []
    assert "cookie-secret" not in (result.detail or "")


def test_exact_request_calls_transport_once_with_post_attempt_identity():
    transport = FakeTransport({"code": "OK", "data": {}})
    adapter = make_adapter(transport)

    result = adapter.execute(request())

    assert len(transport.calls) == 1
    assert transport.calls[0] == {
        "steam_cookie_string": SECRET,
        "buff_order_id": ORDER_ID,
        "steam_id": STEAM_ID,
        "timeout_seconds": 6.5,
    }
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "offer_created_unproven"
    assert result.evidence is None


@pytest.mark.parametrize(
    "outcome",
    [
        {"code": "ERROR", "error": "rejected"},
        {"code": "Login Required"},
        {},
        [],
    ],
)
def test_any_returned_post_result_never_manufactures_success(outcome):
    transport = FakeTransport(outcome)
    adapter = make_adapter(transport)

    result = adapter.execute(request())

    assert len(transport.calls) == 1
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "write_result_unknown"
    assert result.evidence is None


def test_write_result_unknown_maps_to_unknown_without_retry():
    transport = FakeTransport(
        BuffWriteResultUnknown(
            "network unknown",
            method="POST",
            url="https://buff.163.com/api/market/manual_plus/buyer_send_offer",
        )
    )
    adapter = make_adapter(transport)

    result = adapter.execute(request())

    assert len(transport.calls) == 1
    assert result.status is PlatformResultStatus.RESULT_UNKNOWN
    assert result.detail == "write_result_unknown"
    assert result.evidence is None


@pytest.mark.parametrize(
    "error",
    [
        BuffBuyerSendError("invalid_steam_cookie"),
        RuntimeError("session-secret login-secret"),
    ],
)
def test_transport_exceptions_are_sanitized_and_never_retry(error):
    transport = FakeTransport(error)
    adapter = make_adapter(transport)

    result = adapter.execute(request())

    assert len(transport.calls) == 1
    assert result.status in {
        PlatformResultStatus.FAILURE,
        PlatformResultStatus.RESULT_UNKNOWN,
    }
    assert result.evidence is None
    assert "session-secret" not in (result.detail or "")
    assert "login-secret" not in (result.detail or "")


def test_invalid_request_object_fails_closed():
    adapter = make_adapter()
    with pytest.raises(PlatformAdapterProtocolError):
        adapter.execute(object())
