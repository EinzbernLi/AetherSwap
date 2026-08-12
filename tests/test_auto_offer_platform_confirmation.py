from dataclasses import replace

import pytest

from app.auto_offer.adapters import (
    ConfirmOfferEvidence,
    DEFAULT_PLATFORM_CAPABILITIES,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
)
from app.auto_offer.contracts import DeliveryMode, DeliverySnapshot, DeliveryStatus
from app.auto_offer.coordinator import (
    ConfirmOfferStepResult,
    DeliveryCoordinator,
    ReadOnlyCoordinatorConflictError,
    ReadOnlyCoordinatorError,
)
from app.auto_offer.platform_confirmation import SteamTradeOfferConfirmationAdapter
from app.auto_offer.steam_confirmation_transport import (
    SteamConfirmationTransportAuthError,
    SteamConfirmationTransportError,
    SteamConfirmationWriteResultUnknown,
)
from app.auto_offer.store import AutoOfferStoreStaleWriteError, StoredDelivery


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


def _required_delivery(*, revision=5):
    return StoredDelivery(
        DeliverySnapshot(
            purchase_id="buff:order-1",
            buff_order_id="order-1",
            account_id=ACCOUNT_ID,
            recipient_steam_id=STEAM_ID,
            delivery_mode=DeliveryMode.BUYER_SENDS_OFFER,
            delivery_status=DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
            steam_tradeoffer_id=OFFER_ID,
            offer_attempted_at=1.0,
            offer_sent_at=2.0,
            received_at=None,
            delivery_error=None,
            pending_receipt=True,
            assetid=None,
        ),
        revision,
    )


class MemoryStore:
    def __init__(self, current, events=None):
        self.current = current
        self.events = [] if events is None else events
        self.advance_calls = []

    def get_by_purchase_id(self, purchase_id):
        assert purchase_id == self.current.snapshot.purchase_id
        return self.current

    def advance(self, current, target):
        assert current == self.current
        self.events.append(
            (
                "advance",
                current.snapshot.delivery_status,
                target.delivery_status,
                current.revision,
            )
        )
        self.advance_calls.append((current, target))
        self.current = StoredDelivery(target, current.revision + 1)
        return self.current


class RecordingConfirmAdapter:
    capabilities = frozenset({PlatformCapability.CONFIRM_OFFER})

    def __init__(self, store, result_status=PlatformResultStatus.SUCCESS, events=None):
        self.store = store
        self.result_status = result_status
        self.events = [] if events is None else events
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        self.events.append(
            (
                "execute",
                self.store.current.snapshot.delivery_status,
                self.store.current.revision,
                request.revision,
            )
        )
        evidence = None
        if self.result_status is PlatformResultStatus.SUCCESS:
            evidence = ConfirmOfferEvidence(
                steam_tradeoffer_id=request.steam_tradeoffer_id,
                account_steam_id=request.recipient_steam_id,
            )
        return PlatformResult(
            request=request,
            status=self.result_status,
            detail="task034_fake_confirmation",
            evidence=evidence,
        )


def _coordinator(store, adapter):
    return DeliveryCoordinator(
        store,
        {PlatformCapability.CONFIRM_OFFER: adapter},
        timeout_seconds=15.0,
        allow_writes=True,
        allow_confirmation_writes=True,
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


def test_task029_default_write_gate_still_does_not_wire_confirmation():
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


def test_confirmation_gate_cannot_be_enabled_without_general_write_gate():
    current = _required_delivery()
    store = MemoryStore(current)
    adapter = RecordingConfirmAdapter(store)

    with pytest.raises(
        ReadOnlyCoordinatorError,
        match="confirmation_writes_require_allow_writes",
    ):
        DeliveryCoordinator(
            store,
            {PlatformCapability.CONFIRM_OFFER: adapter},
            timeout_seconds=15.0,
            allow_confirmation_writes=True,
        )
    assert adapter.calls == []


def test_confirmation_adapter_registry_must_be_confirm_only():
    current = _required_delivery()
    store = MemoryStore(current)

    class UnsafeAdapter(RecordingConfirmAdapter):
        capabilities = frozenset(
            {
                PlatformCapability.CONFIRM_OFFER,
                PlatformCapability.READ_STEAM_TRADE_OFFER,
            }
        )

    adapter = UnsafeAdapter(store)
    with pytest.raises(ReadOnlyCoordinatorError, match="adapter_capability_mismatch"):
        _coordinator(store, adapter)
    assert adapter.calls == []


def test_runtime_confirmation_persists_attempt_before_exact_adapter_call():
    events = []
    current = _required_delivery()
    store = MemoryStore(current, events)
    adapter = RecordingConfirmAdapter(store, events=events)
    coordinator = _coordinator(store, adapter)

    result = coordinator.step(current)

    assert type(result) is ConfirmOfferStepResult
    assert result.before == current
    assert result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    assert result.attempted.revision == current.revision + 1
    assert result.platform_result.request.revision == result.attempted.revision
    assert result.platform_result.request.capability is PlatformCapability.CONFIRM_OFFER
    assert result.platform_result.request.steam_tradeoffer_id == OFFER_ID
    assert result.platform_result.request.account_id == ACCOUNT_ID
    assert result.platform_result.request.recipient_steam_id == STEAM_ID
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMED
    assert result.after.revision == result.attempted.revision + 1
    assert events == [
        (
            "advance",
            DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
            DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            5,
        ),
        ("execute", DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED, 6, 6),
        (
            "advance",
            DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
            DeliveryStatus.OFFER_CONFIRMED,
            6,
        ),
    ]
    assert len(adapter.calls) == 1


def test_confirmation_result_unknown_persists_exact_bound_unknown_and_never_retries_in_step():
    current = _required_delivery()
    store = MemoryStore(current)
    adapter = RecordingConfirmAdapter(store, PlatformResultStatus.RESULT_UNKNOWN)
    coordinator = _coordinator(store, adapter)

    result = coordinator.step(current)

    assert type(result) is ConfirmOfferStepResult
    assert result.attempted.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    assert result.after.snapshot.delivery_status is DeliveryStatus.RESULT_UNKNOWN
    assert result.after.snapshot.delivery_error == "write_result_unknown"
    assert result.after.snapshot.steam_tradeoffer_id == OFFER_ID
    assert result.after.revision == current.revision + 2
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 2


@pytest.mark.parametrize(
    "status",
    [
        PlatformResultStatus.TIMEOUT,
        PlatformResultStatus.FAILURE,
        PlatformResultStatus.MALFORMED,
        PlatformResultStatus.UNSUPPORTED,
    ],
)
def test_confirmation_known_preflight_failure_leaves_durable_attempt_without_second_write(status):
    current = _required_delivery()
    store = MemoryStore(current)
    adapter = RecordingConfirmAdapter(store, status)
    coordinator = _coordinator(store, adapter)

    result = coordinator.step(current)

    assert type(result) is ConfirmOfferStepResult
    assert result.after == result.attempted
    assert result.after.snapshot.delivery_status is DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED
    assert result.after.revision == current.revision + 1
    assert len(adapter.calls) == 1
    assert len(store.advance_calls) == 1


def test_confirmation_attempt_cas_failure_blocks_before_adapter_execution():
    current = _required_delivery()

    class FailingStore(MemoryStore):
        def advance(self, _current, _target):
            raise AutoOfferStoreStaleWriteError("stale")

    store = FailingStore(current)
    adapter = RecordingConfirmAdapter(store)
    coordinator = _coordinator(store, adapter)

    with pytest.raises(ReadOnlyCoordinatorConflictError, match="stale_write"):
        coordinator.step(current)
    assert adapter.calls == []
