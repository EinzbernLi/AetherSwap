import ast
import importlib
import math
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.auto_offer.adapters import (
    CompletedTradeItemEvidence,
    DEFAULT_PLATFORM_CAPABILITIES,
    DeliveryDirectionEvidence,
    FakePlatformAdapter,
    InventoryStateEvidence,
    OfferStateEvidence,
    PlatformAdapter,
    PlatformAdapterError,
    PlatformAdapterProtocolError,
    PlatformAdapterTimeoutError,
    PlatformCapability,
    PlatformRequest,
    PlatformResult,
    PlatformResultStatus,
    RecipientInventoryItemEvidence,
    SteamCompletedTradeEvidence,
    SteamTradeOfferEvidence,
    SteamTradeOfferLifecycle,
    TradeOfferItemEvidence,
)


def request(**changes):
    value = PlatformRequest(
        purchase_id="purchase-1",
        buff_order_id="buff-order-1",
        account_id="account-1",
        recipient_steam_id="steam-1",
        revision=1,
        capability=PlatformCapability.READ_OFFER_STATE,
        timeout_seconds=5.0,
    )
    return replace(value, **changes)


def test_import_and_fake_constructor_have_no_cwd_or_thread_side_effects(monkeypatch, tmp_path):
    module_name = "app.auto_offer.adapters"
    before = tuple(tmp_path.iterdir())
    thread_count = threading.active_count()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    adapter = module.FakePlatformAdapter()

    assert tuple(tmp_path.iterdir()) == before
    assert threading.active_count() == thread_count
    assert isinstance(adapter, PlatformAdapter)


def test_implementation_has_no_platform_or_runtime_imports():
    source = Path(importlib.import_module("app.auto_offer.adapters").__file__).read_text(
        encoding="utf-8"
    )
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported.isdisjoint(
        {
            "aiohttp",
            "buff",
            "httpx",
            "random",
            "requests",
            "socket",
            "sqlite3",
            "steam",
            "threading",
            "time",
        }
    )
    assert "sleep(" not in source


def test_request_and_result_are_immutable_and_preserve_exact_identity():
    original = request()
    result = PlatformResult(
        original,
        PlatformResultStatus.SUCCESS,
        evidence=OfferStateEvidence("offer-1"),
    )

    with pytest.raises(FrozenInstanceError):
        original.revision = 2
    with pytest.raises(FrozenInstanceError):
        result.status = PlatformResultStatus.FAILURE

    assert result.request is original
    assert (
        result.request.purchase_id,
        result.request.buff_order_id,
        result.request.account_id,
        result.request.recipient_steam_id,
        result.request.revision,
    ) == ("purchase-1", "buff-order-1", "account-1", "steam-1", 1)
    assert result.is_success is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purchase_id", ""),
        ("buff_order_id", " buff-order-1"),
        ("account_id", "account-1 "),
        ("recipient_steam_id", 42),
    ],
)
def test_request_rejects_invalid_exact_identity(field, value):
    with pytest.raises(PlatformAdapterProtocolError):
        request(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_request_rejects_non_integer_or_nonpositive_revision(value):
    with pytest.raises(PlatformAdapterProtocolError):
        request(revision=value)


def test_request_rejects_unknown_capability():
    with pytest.raises(PlatformAdapterProtocolError):
        request(capability="send_offer")


@pytest.mark.parametrize("value", [0, -1.0, True, math.inf, -math.inf, math.nan])
def test_request_rejects_invalid_timeout(value):
    with pytest.raises(PlatformAdapterProtocolError):
        request(timeout_seconds=value)


def test_capability_declaration_is_explicit_and_immutable():
    adapter = FakePlatformAdapter()

    assert adapter.capabilities == DEFAULT_PLATFORM_CAPABILITIES
    assert PlatformCapability.SEND_OFFER not in adapter.capabilities
    with pytest.raises(AttributeError):
        adapter.capabilities.add(PlatformCapability.SEND_OFFER)


def test_unknown_default_and_unsupported_capability_fail_closed():
    adapter = FakePlatformAdapter()

    unknown = adapter.execute(request())
    unsupported = adapter.execute(
        request(capability=PlatformCapability.SEND_OFFER)
    )

    assert unknown.status is PlatformResultStatus.RESULT_UNKNOWN
    assert unknown.is_success is False
    assert unsupported.status is PlatformResultStatus.UNSUPPORTED
    assert unsupported.is_success is False


def test_fake_bare_success_fails_closed_for_the_same_exact_request():
    item = request()
    adapter = FakePlatformAdapter(
        outcomes={item: PlatformResultStatus.SUCCESS},
    )

    first = adapter.execute(item)
    second = adapter.execute(item)

    assert first == second
    assert first.request is item
    assert first.status is PlatformResultStatus.MALFORMED
    assert first.detail == "success_evidence_required"


@pytest.mark.parametrize(
    ("capability", "evidence"),
    [
        (PlatformCapability.READ_DELIVERY_DIRECTION, DeliveryDirectionEvidence()),
        (PlatformCapability.READ_OFFER_STATE, OfferStateEvidence("offer-1")),
        (
            PlatformCapability.READ_INVENTORY_STATE,
            InventoryStateEvidence(("asset-2", "asset-1"), 2),
        ),
    ],
)
def test_fake_adapter_accepts_matching_typed_success_evidence(capability, evidence):
    item = request(capability=capability)
    result = FakePlatformAdapter(
        outcomes={item: PlatformResult(item, PlatformResultStatus.SUCCESS, evidence=evidence)}
    ).execute(item)

    assert result.status is PlatformResultStatus.SUCCESS
    assert result.evidence == evidence


def test_simulated_timeout_is_not_success_and_never_waits():
    item = request()
    adapter = FakePlatformAdapter(
        outcomes={item: PlatformAdapterTimeoutError("simulated")},
    )

    result = adapter.execute(item)

    assert result.status is PlatformResultStatus.TIMEOUT
    assert result.is_success is False


def test_malformed_and_identity_mismatched_results_fail_closed():
    item = request()
    wrong_identity = request(purchase_id="purchase-2")
    adapter = FakePlatformAdapter(
        outcomes={
            item: object(),
            wrong_identity: PlatformResult(
                item,
                PlatformResultStatus.SUCCESS,
                evidence=OfferStateEvidence("offer-1"),
            ),
        }
    )

    malformed = adapter.execute(item)
    mismatched = adapter.execute(wrong_identity)

    assert malformed.status is PlatformResultStatus.MALFORMED
    assert mismatched.status is PlatformResultStatus.MALFORMED
    assert not malformed.is_success
    assert not mismatched.is_success


def test_unrecognized_internal_error_and_protocol_error_fail_closed():
    item = request()
    protocol_item = request(revision=2)
    adapter = FakePlatformAdapter(
        outcomes={
            item: RuntimeError("unexpected"),
            protocol_item: PlatformAdapterError("invalid result"),
        }
    )

    failure = adapter.execute(item)
    malformed = adapter.execute(protocol_item)

    assert failure.status is PlatformResultStatus.FAILURE
    assert malformed.status is PlatformResultStatus.MALFORMED
    assert not failure.is_success
    assert not malformed.is_success


def test_fake_rejects_invalid_requests_instead_of_returning_success():
    with pytest.raises(PlatformAdapterProtocolError):
        FakePlatformAdapter().execute(object())


def test_fake_does_not_import_or_mutate_store_or_delivery_snapshot():
    module = importlib.import_module("app.auto_offer.adapters")
    item = request()
    before = item

    result = FakePlatformAdapter(
        outcomes={
            item: PlatformResult(
                item,
                PlatformResultStatus.SUCCESS,
                evidence=OfferStateEvidence("offer-1"),
            )
        },
    ).execute(item)

    assert "StoredDelivery" not in module.__dict__
    assert "DeliverySnapshot" not in module.__dict__
    assert item == before
    assert result.request == before


@pytest.mark.parametrize(
    ("evidence", "capability"),
    [
        (DeliveryDirectionEvidence(), PlatformCapability.READ_OFFER_STATE),
        (OfferStateEvidence("offer-1"), PlatformCapability.READ_INVENTORY_STATE),
        (InventoryStateEvidence(("asset-1",)), PlatformCapability.READ_DELIVERY_DIRECTION),
    ],
)
def test_success_evidence_must_match_request_capability(evidence, capability):
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(request(capability=capability), PlatformResultStatus.SUCCESS, evidence=evidence)


def test_non_success_or_send_offer_cannot_contain_success_evidence():
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(),
            PlatformResultStatus.RESULT_UNKNOWN,
            evidence=OfferStateEvidence("offer-1"),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(capability=PlatformCapability.SEND_OFFER),
            PlatformResultStatus.SUCCESS,
            evidence=OfferStateEvidence("offer-1"),
        )


def test_fake_forged_wrong_evidence_type_stays_malformed():
    item = request()
    forged = object.__new__(PlatformResult)
    object.__setattr__(forged, "request", item)
    object.__setattr__(forged, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged, "detail", None)
    object.__setattr__(forged, "evidence", DeliveryDirectionEvidence())

    result = FakePlatformAdapter(outcomes={item: forged}).execute(item)

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "evidence_type_mismatch"
    assert result.evidence is None


def test_evidence_values_are_immutable_validated_and_canonical():
    inventory = InventoryStateEvidence(("asset-2", "asset-1"), 2)
    assert inventory.assetids == ("asset-1", "asset-2")
    with pytest.raises(FrozenInstanceError):
        inventory.assetids = ()
    with pytest.raises(PlatformAdapterProtocolError):
        DeliveryDirectionEvidence("buyer_sends_offer")
    with pytest.raises(PlatformAdapterProtocolError):
        OfferStateEvidence(" offer-1")
    for assetids, total in (
        (["asset-1"], None),
        ((True,), None),
        (("asset-1", "asset-1"), None),
        (("asset-1",), 0),
        ((), True),
    ):
        with pytest.raises(PlatformAdapterProtocolError):
            InventoryStateEvidence(assetids, total)


def steam_request(**changes):
    values = {
        "capability": PlatformCapability.READ_STEAM_TRADE_OFFER,
        "steam_tradeoffer_id": "offer-1",
    }
    values.update(changes)
    return request(**values)


def trade_item(**changes):
    value = TradeOfferItemEvidence(
        appid=730,
        contextid="2",
        assetid="asset-1",
        amount=1,
    )
    return replace(value, **changes)


def trade_evidence(**changes):
    value = SteamTradeOfferEvidence(
        steam_tradeoffer_id="offer-1",
        account_steam_id="steam-1",
        counterparty_steam_id="steam-2",
        is_our_offer=False,
        lifecycle=SteamTradeOfferLifecycle.ACTIVE,
        items_to_give=(),
        items_to_receive=(trade_item(),),
    )
    return replace(value, **changes)


def completed_trade_request(**changes):
    return request(
        capability=PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        steam_tradeoffer_id="offer-1",
        **changes,
    )


def completed_trade_item(**changes):
    value = CompletedTradeItemEvidence(
        appid=730,
        contextid="2",
        assetid="source-1",
        amount=1,
        new_contextid="3",
        new_assetid="new-1",
    )
    return replace(value, **changes)


def recipient_inventory_item(**changes):
    value = RecipientInventoryItemEvidence(
        appid=730,
        contextid="3",
        assetid="new-1",
        amount=1,
    )
    return replace(value, **changes)


def completed_trade_evidence(**changes):
    value = SteamCompletedTradeEvidence(
        steam_tradeoffer_id="offer-1",
        steam_trade_id="trade-1",
        account_steam_id="steam-1",
        counterparty_steam_id="steam-2",
        completed_at=100.0,
        items_given=(),
        items_received=(completed_trade_item(),),
        inventory_confirmed_items=(recipient_inventory_item(),),
    )
    return replace(value, **changes)


def test_steam_trade_offer_request_binding_is_exact_and_legacy_safe():
    item = steam_request()
    assert item.steam_tradeoffer_id == "offer-1"
    assert request().steam_tradeoffer_id is None
    with pytest.raises(PlatformAdapterProtocolError):
        request(capability=PlatformCapability.READ_STEAM_TRADE_OFFER)
    with pytest.raises(PlatformAdapterProtocolError):
        request(steam_tradeoffer_id="offer-1")
    with pytest.raises(PlatformAdapterProtocolError):
        request(capability=PlatformCapability.SEND_OFFER, steam_tradeoffer_id="offer-1")
    for value in ("", " offer-1", "offer-1 ", True, 1):
        with pytest.raises(PlatformAdapterProtocolError):
            steam_request(steam_tradeoffer_id=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", 0),
        ("timeout_seconds", math.nan),
        ("capability", "read_steam_trade_offer"),
    ],
)
def test_platform_result_revalidates_forged_nested_request(field, value):
    forged = object.__new__(PlatformRequest)
    source = steam_request()
    for name in (
        "purchase_id",
        "buff_order_id",
        "account_id",
        "recipient_steam_id",
        "revision",
        "capability",
        "timeout_seconds",
        "steam_tradeoffer_id",
    ):
        object.__setattr__(forged, name, getattr(source, name))
    object.__setattr__(forged, field, value)
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(forged, PlatformResultStatus.RESULT_UNKNOWN)


@pytest.mark.parametrize(
    "changes",
    [
        {"steam_tradeoffer_id": "offer-2"},
        {"account_steam_id": "steam-3"},
    ],
)
def test_platform_result_rejects_cross_bound_trade_offer_evidence(changes):
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            steam_request(),
            PlatformResultStatus.SUCCESS,
            evidence=trade_evidence(**changes),
        )


def test_platform_result_rejects_forged_missing_or_legacy_trade_offer_id():
    missing = object.__new__(PlatformRequest)
    source = steam_request()
    for name in (
        "purchase_id",
        "buff_order_id",
        "account_id",
        "recipient_steam_id",
        "revision",
        "capability",
        "timeout_seconds",
    ):
        object.__setattr__(missing, name, getattr(source, name))
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(missing, PlatformResultStatus.RESULT_UNKNOWN)

    legacy = object.__new__(PlatformRequest)
    source = request()
    for name in (
        "purchase_id",
        "buff_order_id",
        "account_id",
        "recipient_steam_id",
        "revision",
        "capability",
        "timeout_seconds",
    ):
        object.__setattr__(legacy, name, getattr(source, name))
    object.__setattr__(legacy, "steam_tradeoffer_id", "offer-1")
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(legacy, PlatformResultStatus.RESULT_UNKNOWN)


def test_steam_trade_offer_evidence_is_frozen_typed_and_canonical():
    first = trade_item(assetid="asset-1")
    second = trade_item(assetid="asset-2")
    evidence = trade_evidence(
        items_to_receive=(second, first),
        items_to_give=(trade_item(appid=440, assetid="asset-3"),),
    )
    assert evidence.items_to_receive == (first, second)
    assert evidence.items_to_give[0].appid == 440
    with pytest.raises(FrozenInstanceError):
        evidence.lifecycle = SteamTradeOfferLifecycle.ACCEPTED


@pytest.mark.parametrize(
    "value",
    [
        {"appid": True},
        {"appid": 0},
        {"contextid": " context"},
        {"assetid": ""},
        {"amount": False},
        {"amount": 0},
    ],
)
def test_trade_offer_item_evidence_rejects_invalid_fields(value):
    with pytest.raises(PlatformAdapterProtocolError):
        trade_item(**value)


def test_steam_trade_offer_evidence_rejects_identity_duplicates_and_empty_offer():
    with pytest.raises(PlatformAdapterProtocolError):
        trade_evidence(counterparty_steam_id="steam-1")
    with pytest.raises(PlatformAdapterProtocolError):
        trade_evidence(items_to_receive=(trade_item(), trade_item()))
    with pytest.raises(PlatformAdapterProtocolError):
        trade_evidence(items_to_give=(), items_to_receive=())


def test_platform_result_accepts_new_typed_success_and_rejects_mismatch():
    item = steam_request()
    result = PlatformResult(
        item,
        PlatformResultStatus.SUCCESS,
        detail="trade_offer_active",
        evidence=trade_evidence(),
    )
    assert result.is_success is True
    assert result.evidence.lifecycle is SteamTradeOfferLifecycle.ACTIVE
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(),
            PlatformResultStatus.SUCCESS,
            evidence=trade_evidence(),
        )


def test_fake_adapter_rejects_forged_nested_platform_result():
    item = steam_request()
    forged_request = object.__new__(PlatformRequest)
    for name in (
        "purchase_id",
        "buff_order_id",
        "account_id",
        "recipient_steam_id",
        "revision",
        "capability",
        "timeout_seconds",
        "steam_tradeoffer_id",
    ):
        object.__setattr__(forged_request, name, getattr(item, name))
    object.__delattr__(forged_request, "steam_tradeoffer_id")
    forged_result = object.__new__(PlatformResult)
    object.__setattr__(forged_result, "request", forged_request)
    object.__setattr__(forged_result, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged_result, "detail", "trade_offer_active")
    object.__setattr__(forged_result, "evidence", trade_evidence())

    result = FakePlatformAdapter(
        capabilities={PlatformCapability.READ_STEAM_TRADE_OFFER},
        outcomes={item: forged_result},
    ).execute(item)

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.evidence is None


@pytest.mark.parametrize(
    "changes",
    [
        {"steam_tradeoffer_id": "offer-2"},
        {"account_steam_id": "steam-3"},
    ],
)
def test_fake_adapter_rejects_cross_bound_forged_evidence(changes):
    item = steam_request()
    forged_result = object.__new__(PlatformResult)
    object.__setattr__(forged_result, "request", item)
    object.__setattr__(forged_result, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged_result, "detail", "trade_offer_active")
    object.__setattr__(forged_result, "evidence", trade_evidence(**changes))

    result = FakePlatformAdapter(
        capabilities={PlatformCapability.READ_STEAM_TRADE_OFFER},
        outcomes={item: forged_result},
    ).execute(item)

    assert result.status is PlatformResultStatus.MALFORMED
    assert result.detail == "evidence_type_mismatch"
    assert result.evidence is None


def test_completed_trade_capability_is_additive_and_requires_exact_offer_id():
    assert PlatformCapability.READ_STEAM_COMPLETED_TRADE.value == (
        "read_steam_completed_trade"
    )
    assert PlatformCapability.READ_STEAM_TRADE_OFFER.value == "read_steam_trade_offer"
    assert request().steam_tradeoffer_id is None
    assert completed_trade_request().steam_tradeoffer_id == "offer-1"
    with pytest.raises(PlatformAdapterProtocolError):
        request(
            capability=PlatformCapability.READ_STEAM_COMPLETED_TRADE,
        )
    with pytest.raises(PlatformAdapterProtocolError):
        request(
            capability=PlatformCapability.READ_OFFER_STATE,
            steam_tradeoffer_id="offer-1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("appid", True),
        ("appid", 0),
        ("contextid", " context"),
        ("assetid", ""),
        ("amount", False),
        ("amount", 0),
        ("new_contextid", "context "),
        ("new_assetid", 1),
    ],
)
def test_completed_trade_item_evidence_is_frozen_and_strict(field, value):
    with pytest.raises(PlatformAdapterProtocolError):
        completed_trade_item(**{field: value})
    item = completed_trade_item()
    with pytest.raises(FrozenInstanceError):
        item.assetid = "other"


def test_recipient_inventory_item_evidence_is_frozen_and_strict():
    item = recipient_inventory_item()
    with pytest.raises(FrozenInstanceError):
        item.assetid = "other"
    for field, value in (
        ("appid", True),
        ("contextid", " context"),
        ("assetid", ""),
        ("amount", False),
    ):
        with pytest.raises(PlatformAdapterProtocolError):
            recipient_inventory_item(**{field: value})


def test_completed_trade_evidence_is_immutable_canonical_and_multi_item_safe():
    first = completed_trade_item(assetid="source-1", new_assetid="new-1")
    second = completed_trade_item(
        appid=440,
        assetid="source-2",
        new_assetid="new-2",
    )
    evidence = completed_trade_evidence(
        items_received=(second, first),
        inventory_confirmed_items=(
            recipient_inventory_item(appid=440, assetid="new-2"),
            recipient_inventory_item(),
        ),
    )
    assert [item.appid for item in evidence.items_received] == [440, 730]
    assert evidence.items_received == (second, first)
    assert evidence.items_given == ()
    assert not hasattr(evidence, "purchase_assetid")
    assert not hasattr(evidence, "selected_assetid")
    with pytest.raises(FrozenInstanceError):
        evidence.completed_at = 101.0


@pytest.mark.parametrize(
    "changes",
    [
        {"items_received": ()},
        {
            "items_received": (
                completed_trade_item(),
                completed_trade_item(),
            )
        },
        {
            "items_received": (
                completed_trade_item(),
            ),
            "inventory_confirmed_items": (
                recipient_inventory_item(assetid="not-received"),
            ),
        },
    ],
)
def test_completed_trade_evidence_rejects_empty_duplicates_and_invalid_subset(changes):
    with pytest.raises(PlatformAdapterProtocolError):
        completed_trade_evidence(**changes)


def test_completed_trade_evidence_rejects_duplicate_post_trade_and_inventory_identity():
    with pytest.raises(PlatformAdapterProtocolError):
        completed_trade_evidence(
            items_received=(
                completed_trade_item(assetid="source-1", new_assetid="new-1"),
                completed_trade_item(assetid="source-2", new_assetid="new-1"),
            ),
            inventory_confirmed_items=(),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        completed_trade_evidence(
            inventory_confirmed_items=(
                recipient_inventory_item(),
                recipient_inventory_item(),
            )
        )


def test_platform_result_maps_completed_trade_and_cross_binds_both_identities():
    result = PlatformResult(
        completed_trade_request(),
        PlatformResultStatus.SUCCESS,
        detail="completed_trade_proven",
        evidence=completed_trade_evidence(),
    )
    assert result.is_success is True
    assert type(result.evidence) is SteamCompletedTradeEvidence
    for changes in (
        {"steam_tradeoffer_id": "other-offer"},
        {"account_steam_id": "other-steam"},
    ):
        with pytest.raises(PlatformAdapterProtocolError):
            PlatformResult(
                completed_trade_request(),
                PlatformResultStatus.SUCCESS,
                evidence=completed_trade_evidence(**changes),
            )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            completed_trade_request(),
            PlatformResultStatus.RESULT_UNKNOWN,
            evidence=completed_trade_evidence(),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(capability=PlatformCapability.SEND_OFFER),
            PlatformResultStatus.SUCCESS,
            evidence=completed_trade_evidence(),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            request(),
            PlatformResultStatus.SUCCESS,
            evidence=completed_trade_evidence(),
        )
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            completed_trade_request(),
            PlatformResultStatus.SUCCESS,
            evidence=trade_evidence(),
        )


@pytest.mark.parametrize("completed_at", [True, -1, math.nan, math.inf, "100"])
def test_completed_trade_evidence_rejects_invalid_completed_at(completed_at):
    with pytest.raises(PlatformAdapterProtocolError):
        completed_trade_evidence(completed_at=completed_at)


def test_platform_result_defensively_revalidates_forged_completed_trade_evidence():
    forged_item = object.__new__(CompletedTradeItemEvidence)
    for name, value in (
        ("appid", 730),
        ("contextid", "2"),
        ("assetid", "source-1"),
        ("amount", 1),
        ("new_contextid", "3"),
        ("new_assetid", None),
    ):
        object.__setattr__(forged_item, name, value)
    forged = object.__new__(SteamCompletedTradeEvidence)
    for name, value in (
        ("steam_tradeoffer_id", "offer-1"),
        ("steam_trade_id", "trade-1"),
        ("account_steam_id", "steam-1"),
        ("counterparty_steam_id", "steam-2"),
        ("completed_at", 100.0),
        ("items_given", ()),
        ("items_received", (forged_item,)),
        ("inventory_confirmed_items", ()),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(PlatformAdapterProtocolError):
        PlatformResult(
            completed_trade_request(),
            PlatformResultStatus.SUCCESS,
            evidence=forged,
        )


def test_fake_adapter_supports_explicit_completed_trade_and_rejects_bare_success():
    item = completed_trade_request()
    typed = FakePlatformAdapter(
        capabilities={PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        outcomes={
            item: PlatformResult(
                item,
                PlatformResultStatus.SUCCESS,
                evidence=completed_trade_evidence(),
            )
        },
    ).execute(item)
    assert typed.status is PlatformResultStatus.SUCCESS
    assert type(typed.evidence) is SteamCompletedTradeEvidence

    bare = FakePlatformAdapter(
        capabilities={PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        outcomes={item: PlatformResultStatus.SUCCESS},
    ).execute(item)
    assert bare.status is PlatformResultStatus.MALFORMED
    assert bare.detail == "success_evidence_required"


@pytest.mark.parametrize(
    "changes",
    [
        {"steam_tradeoffer_id": "other-offer"},
        {"account_steam_id": "other-steam"},
    ],
)
def test_fake_adapter_rejects_forged_cross_bound_completed_trade_evidence(changes):
    item = completed_trade_request()
    forged = object.__new__(PlatformResult)
    object.__setattr__(forged, "request", item)
    object.__setattr__(forged, "status", PlatformResultStatus.SUCCESS)
    object.__setattr__(forged, "detail", "completed_trade_proven")
    object.__setattr__(forged, "evidence", completed_trade_evidence(**changes))
    result = FakePlatformAdapter(
        capabilities={PlatformCapability.READ_STEAM_COMPLETED_TRADE},
        outcomes={item: forged},
    ).execute(item)
    assert result.status is PlatformResultStatus.MALFORMED
    assert result.evidence is None
