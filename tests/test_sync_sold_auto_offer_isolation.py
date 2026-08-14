from contextlib import contextmanager
from dataclasses import replace
import inspect

import pytest

import app.auto_offer.host_ownership as ownership_module
import app.config_loader as config_loader
import app.inventory_cs2 as inventory_module
import app.state as state_module
import app.steam_listings as listings_module
import app.sync_sold as sync_sold
from app.auto_offer.host_ownership import (
    HostPurchaseMutationBlockedError,
    HostPurchaseOwnership,
    HostPurchaseOwnershipDecision,
)
from app.database import Purchase, _purchase_to_dict


def _purchase(db_id, name="Knife", **changes):
    value = {
        "_db_id": db_id,
        "name": name,
        "buff_order_id": None,
        "assetid": None,
        "pending_receipt": None,
        "sale_price": None,
        "sold_at": None,
        "listing": False,
        "listing_status": None,
    }
    value.update(changes)
    return value


def _decision(ownership):
    return HostPurchaseOwnershipDecision(ownership, None, ownership.value)


def _sparse_purchase(db_id=7):
    """Match the real serializer: SQL NULL optional fields are omitted."""
    return _purchase_to_dict(
        Purchase(id=db_id, name="Knife", goods_id=1, price=1.0, at=1.0)
    )


class _FakeState:
    def __init__(self, purchases, sales=None, final_failures=None):
        self.purchases = purchases
        self.sales = sales or []
        self.calls = []
        self.final_failures = list(final_failures or [])

    def get_purchases(self):
        return self.purchases

    def get_sales(self):
        return self.sales

    def update_purchase_by_id_if_matches(self, db_id, data, expected):
        self.calls.append((db_id, dict(data), dict(expected)))
        if self.final_failures:
            failure = self.final_failures.pop(0)
            if failure is not None:
                raise HostPurchaseMutationBlockedError(failure)
        purchase = next(p for p in self.purchases if p.get("_db_id") == db_id)
        for field, value in expected.items():
            if purchase.get(field) != value:
                return False
        purchase.update(data)
        return True


def _configure_run(monkeypatch, state, decisions, inventory, sold_map, *, history_ok=True, cookies=None):
    monkeypatch.setattr(state_module, "get_state", lambda: state)
    monkeypatch.setattr(
        ownership_module,
        "classify_host_purchases",
        lambda _purchases: decisions,
    )
    monkeypatch.setattr(
        config_loader,
        "get_steam_credentials",
        lambda: {"cookies": cookies if cookies is not None else {"steamLoginSecure": "ok"}},
    )
    monkeypatch.setattr(
        inventory_module,
        "scan_cs2_inventory",
        lambda: (True, inventory, None),
    )
    monkeypatch.setattr(
        listings_module,
        "fetch_my_history_sold",
        lambda _cookies, debug_fn=None: (
            (True, sold_map, None)
            if history_ok
            else (False, {}, "history failed")
        ),
    )


def test_managed_row_does_not_block_unowned_legacy_fill(monkeypatch):
    managed = _purchase(1)
    legacy = _purchase(2)
    state = _FakeState([managed, legacy])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.MANAGED), _decision(HostPurchaseOwnership.UNOWNED)],
        [{"assetid": "asset-legacy", "market_hash_name": "Knife"}],
        {},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is True
    assert result["filled"] == 1
    assert result["updated"] == 0
    assert [call[0] for call in state.calls] == [2]
    assert state.purchases[0].get("assetid") is None


@pytest.mark.parametrize(
    "ownership",
    [
        HostPurchaseOwnership.MANAGED,
        HostPurchaseOwnership.RECEIPT_PENDING,
        HostPurchaseOwnership.UNSAFE,
        HostPurchaseOwnership.RELEASED,
    ],
)
def test_protected_same_name_rows_never_receive_legacy_fill(monkeypatch, ownership):
    purchase = _purchase(1, assetid="asset-released" if ownership is HostPurchaseOwnership.RELEASED else None)
    state = _FakeState([purchase])
    _configure_run(
        monkeypatch,
        state,
        [_decision(ownership)],
        [{"assetid": "asset-new", "market_hash_name": "Knife"}],
        {},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    if ownership is HostPurchaseOwnership.UNSAFE:
        assert ok is False
        assert result["error"] == "AUTO_OFFER_OWNERSHIP_UNSAFE"
    else:
        assert ok is True
        assert result["filled"] == 0
    assert state.calls == []
    assert purchase.get("assetid") != "asset-new"


def test_released_row_may_receive_exact_asset_sold_update_but_not_fill(monkeypatch):
    purchase = _purchase(1, assetid="asset-released")
    state = _FakeState([purchase])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.RELEASED)],
        [{"assetid": "asset-other", "market_hash_name": "Knife"}],
        {"asset-released": 12.5},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is True
    assert result["filled"] == 0
    assert result["updated"] == 1
    assert state.purchases[0]["assetid"] == "asset-released"
    assert state.calls[0][1] == {
        "sale_price": 12.5,
        "sold_at": state.calls[0][1]["sold_at"],
        "listing": False,
        "listing_status": None,
    }


def test_legacy_duplicate_name_assignment_reserves_all_existing_assetids(monkeypatch):
    protected = _purchase(1, assetid="asset-used")
    first = _purchase(2)
    second = _purchase(3)
    state = _FakeState([protected, first, second])
    _configure_run(
        monkeypatch,
        state,
        [
            _decision(HostPurchaseOwnership.MANAGED),
            _decision(HostPurchaseOwnership.UNOWNED),
            _decision(HostPurchaseOwnership.UNOWNED),
        ],
        [
            {"assetid": "asset-used", "market_hash_name": "Knife"},
            {"assetid": "asset-first", "market_hash_name": "Knife"},
            {"assetid": "asset-second", "market_hash_name": "Knife"},
        ],
        {},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is True
    assert result["filled"] == 2
    assert [call[1]["assetid"] for call in state.calls] == ["asset-first", "asset-second"]


def test_newly_filled_legacy_asset_can_be_marked_sold_same_invocation(monkeypatch):
    purchase = _purchase(1)
    state = _FakeState([purchase])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.UNOWNED)],
        [{"assetid": "asset-new", "market_hash_name": "Knife"}],
        {"asset-new": 22.0},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is True
    assert result["filled"] == 1
    assert result["updated"] == 1
    assert state.calls[0][1]["assetid"] == "asset-new"
    assert state.calls[0][1]["sale_price"] == 22.0


def test_sold_history_failure_writes_zero_planned_fills(monkeypatch):
    state = _FakeState([_purchase(1)])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.UNOWNED)],
        [{"assetid": "asset-new", "market_hash_name": "Knife"}],
        {},
        history_ok=False,
    )

    ok, _result = sync_sold.run_sync_sold_from_history()

    assert ok is False
    assert state.calls == []


def test_invalid_final_cookie_writes_zero_planned_fills(monkeypatch):
    state = _FakeState([_purchase(1)])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.UNOWNED)],
        [{"assetid": "asset-new", "market_hash_name": "Knife"}],
        {},
        cookies={"other": "cookie"},
    )

    ok, _result = sync_sold.run_sync_sold_from_history()

    assert ok is False
    assert state.calls == []


def test_inventory_scan_failure_still_allows_sold_update(monkeypatch):
    purchase = _purchase(1, assetid="asset-existing")
    state = _FakeState([purchase])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.UNOWNED)],
        [],
        {"asset-existing": 8.5},
    )
    monkeypatch.setattr(inventory_module, "scan_cs2_inventory", lambda: (False, [], "offline"))

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is True
    assert result["updated"] == 1
    assert result["filled"] == 0


def test_initial_unsafe_store_failure_writes_nothing(monkeypatch):
    state = _FakeState([_purchase(1)])
    monkeypatch.setattr(state_module, "get_state", lambda: state)
    monkeypatch.setattr(
        config_loader,
        "get_steam_credentials",
        lambda: {"cookies": {"steamLoginSecure": "ok"}},
    )
    monkeypatch.setattr(
        ownership_module,
        "classify_host_purchases",
        lambda _purchases: (_ for _ in ()).throw(
            HostPurchaseMutationBlockedError("AUTO_OFFER_OWNERSHIP_UNSAFE")
        ),
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is False
    assert result["error"] == "AUTO_OFFER_OWNERSHIP_UNSAFE"
    assert state.calls == []


def test_unsafe_final_row_stops_further_writes(monkeypatch):
    state = _FakeState([_purchase(1, assetid="a"), _purchase(2, assetid="b")], final_failures=[None, "AUTO_OFFER_OWNERSHIP_UNSAFE"])
    _configure_run(
        monkeypatch,
        state,
        [_decision(HostPurchaseOwnership.UNOWNED), _decision(HostPurchaseOwnership.UNOWNED)],
        [],
        {"a": 1.0, "b": 2.0},
    )

    ok, result = sync_sold.run_sync_sold_from_history()

    assert ok is False
    assert result["error"] == "AUTO_OFFER_OWNERSHIP_UNSAFE"
    assert [call[0] for call in state.calls] == [1, 2]
    assert state.purchases[0]["sale_price"] == 1.0
    assert state.purchases[1].get("sale_price") is None


def test_sync_sold_has_no_broad_replace_path():
    source = inspect.getsource(sync_sold.run_sync_sold_from_history)
    assert "replace_transactions" not in source
    assert "db_replace_transactions" not in source


def test_batch_classification_loads_one_store_index(monkeypatch):
    purchases = [_purchase(1), _purchase(2)]
    calls = []
    monkeypatch.setattr(
        ownership_module,
        "_store_index",
        lambda _path=None: calls.append(True) or {},
    )

    decisions = ownership_module.classify_host_purchases(purchases)

    assert len(decisions) == 2
    assert calls == [True]


def _patch_state_cas(monkeypatch, current, writes):
    @contextmanager
    def guard(_action, **_kwargs):
        yield

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", lambda: [current])
    monkeypatch.setattr(
        state_module,
        "require_purchase_mutation_allowed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        state_module,
        "db_update_purchase_by_id",
        lambda *_args: writes.append(True) or True,
    )


def test_stable_id_helper_accepts_unchanged_sparse_serializer_purchase(monkeypatch):
    current = _sparse_purchase()
    writes = []
    _patch_state_cas(monkeypatch, current, writes)
    expected = {
        field: current.get(field)
        for field in sync_sold._EXPECTED_PURCHASE_FIELDS
    }

    assert state_module.State().update_purchase_by_id_if_matches(
        7,
        {"listing": False},
        expected,
    ) is True
    assert writes == [True]


def test_stable_id_helper_treats_explicit_none_as_sparse_none(monkeypatch):
    expected_source = _sparse_purchase()
    current = dict(expected_source)
    current["assetid"] = None
    writes = []
    _patch_state_cas(monkeypatch, current, writes)
    expected = {
        field: current.get(field)
        for field in sync_sold._EXPECTED_PURCHASE_FIELDS
        if field != "assetid"
    }

    assert state_module.State().update_purchase_by_id_if_matches(
        7,
        {"listing": False},
        expected,
    ) is True
    assert writes == [True]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assetid", "asset-new"),
        ("pending_receipt", True),
        ("sale_price", 12.0),
        ("listing", True),
        ("listing_status", "active"),
    ],
)
def test_stable_id_helper_rejects_sparse_to_nonnull_stale_changes(
    monkeypatch,
    field,
    value,
):
    expected_source = _sparse_purchase()
    current = dict(expected_source)
    current[field] = value
    writes = []
    _patch_state_cas(monkeypatch, current, writes)
    expected = {
        item: expected_source.get(item)
        for item in sync_sold._EXPECTED_PURCHASE_FIELDS
        if item != field
    }

    assert state_module.State().update_purchase_by_id_if_matches(
        7,
        {"listing": False},
        expected,
    ) is False
    assert writes == []


def test_stable_id_helper_orders_guard_read_ownership_compare_write(monkeypatch):
    events = []

    @contextmanager
    def guard(action, **_kwargs):
        events.append(("guard", action))
        yield

    current = _purchase(7, assetid="old")
    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", lambda: events.append(("read",)) or [current])
    monkeypatch.setattr(
        state_module,
        "require_purchase_mutation_allowed",
        lambda *_args, **_kwargs: events.append(("ownership",)),
    )
    monkeypatch.setattr(
        state_module,
        "db_update_purchase_by_id",
        lambda *_args: events.append(("write",)) or True,
    )

    assert state_module.State().update_purchase_by_id_if_matches(
        7,
        {"listing": False},
        {field: current.get(field) for field in sync_sold._EXPECTED_PURCHASE_FIELDS},
    ) is True
    assert [event[0] for event in events] == ["guard", "read", "ownership", "write"]


@pytest.mark.parametrize("field", ["name", "assetid", "sale_price", "listing"])
def test_stable_id_helper_skips_stale_expected_values(monkeypatch, field):
    current = _purchase(7, assetid="current")
    writes = []
    expected = {item: current.get(item) for item in sync_sold._EXPECTED_PURCHASE_FIELDS}
    expected[field] = "stale-value" if field != "sale_price" else 99.0

    @contextmanager
    def guard(_action, **_kwargs):
        yield

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", lambda: [current])
    monkeypatch.setattr(state_module, "require_purchase_mutation_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(state_module, "db_update_purchase_by_id", lambda *_args: writes.append(True) or True)

    assert state_module.State().update_purchase_by_id_if_matches(
        7,
        {"listing": False},
        expected,
    ) is False
    assert writes == []
