from contextlib import contextmanager

import pytest

import app.routes.transactions as transaction_routes
import app.state as state_module
from app.auto_offer.host_ownership import HostPurchaseMutationBlockedError


def _purchase(**changes):
    value = {
        "_db_id": 7,
        "buff_order_id": "order-1",
        "goods_id": 123,
        "pending_receipt": True,
        "assetid": None,
    }
    value.update(changes)
    return value


def test_update_by_id_inspects_ownership_inside_existing_authority_guard(monkeypatch):
    events = []
    inside = {"value": False}

    @contextmanager
    def guard(action, **_kwargs):
        assert action == "host_transaction_mutation"
        assert not inside["value"]
        inside["value"] = True
        events.append("guard_enter")
        try:
            yield
        finally:
            events.append("guard_exit")
            inside["value"] = False

    def get_purchases():
        assert inside["value"]
        events.append("host_read")
        return [_purchase()]

    def ownership_check(purchase, *, operation, data=None, **_kwargs):
        assert inside["value"]
        assert purchase["_db_id"] == 7
        assert operation == "update"
        assert data == {"listing": True}
        events.append("ownership_check")

    def update(db_id, data):
        assert inside["value"]
        assert db_id == 7
        assert data == {"listing": True}
        events.append("host_write")
        return True

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", get_purchases)
    monkeypatch.setattr(state_module, "require_purchase_mutation_allowed", ownership_check)
    monkeypatch.setattr(state_module, "db_update_purchase_by_id", update)

    assert state_module.State().update_purchase_by_id(7, {"listing": True}) is True
    assert events == [
        "guard_enter",
        "host_read",
        "ownership_check",
        "host_write",
        "guard_exit",
    ]


@pytest.mark.parametrize(
    ("method", "args", "db_name"),
    [
        ("delete_purchase", (0,), "db_delete_purchase"),
        ("delete_purchase_by_id", (7,), "db_delete_purchase_by_id"),
        ("update_purchase", (0, {"pending_receipt": False}), "db_update_purchase"),
        ("update_purchase_by_id", (7, {"assetid": "manual"}), "db_update_purchase_by_id"),
    ],
)
def test_managed_purchase_mutations_stop_before_database_write(
    monkeypatch,
    method,
    args,
    db_name,
):
    @contextmanager
    def guard(_action, **_kwargs):
        yield

    def blocked(*_args, **_kwargs):
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_PURCHASE_MANAGED")

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", lambda: [_purchase()])
    monkeypatch.setattr(state_module, "require_purchase_mutation_allowed", blocked)
    monkeypatch.setattr(
        state_module,
        db_name,
        lambda *_args, **_kwargs: pytest.fail("database mutation must not run"),
    )

    with pytest.raises(HostPurchaseMutationBlockedError) as exc_info:
        getattr(state_module.State(), method)(*args)
    assert exc_info.value.code == "AUTO_OFFER_PURCHASE_MANAGED"


def test_broad_clear_and_replace_preflight_before_database_write(monkeypatch):
    @contextmanager
    def guard(_action, **_kwargs):
        yield

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(state_module, "db_get_purchases", lambda: [_purchase()])

    def blocked(*_args, **_kwargs):
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_PURCHASE_MANAGED")

    monkeypatch.setattr(state_module, "require_broad_transaction_mutation_allowed", blocked)
    monkeypatch.setattr(
        state_module,
        "db_clear_transactions",
        lambda: pytest.fail("clear must not run"),
    )
    monkeypatch.setattr(
        state_module,
        "db_replace_transactions",
        lambda *_args: pytest.fail("replace must not run"),
    )

    state = state_module.State()
    with pytest.raises(HostPurchaseMutationBlockedError):
        state.clear_transactions()
    with pytest.raises(HostPurchaseMutationBlockedError):
        state.replace_transactions([_purchase()], [])


def test_exact_receipt_path_is_not_routed_through_generic_ownership_fence(monkeypatch):
    seen = []

    @contextmanager
    def guard(action, **kwargs):
        seen.append((action, kwargs))
        yield

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(
        state_module,
        "require_purchase_mutation_allowed",
        lambda *_args, **_kwargs: pytest.fail("generic ownership fence must not run"),
    )
    monkeypatch.setattr(
        state_module,
        "db_complete_purchase_receipt_by_id",
        lambda db_id, order_id, assetid: (db_id, order_id, assetid) == (7, "order-1", "asset-1"),
    )

    assert state_module.State().complete_purchase_receipt_by_id(
        7,
        "order-1",
        "asset-1",
    ) is True
    assert seen == [
        (
            "host_receipt",
            {
                "buff_order_id": "order-1",
                "host_db_id": 7,
                "assetid": "asset-1",
            },
        )
    ]


def test_sales_update_is_unchanged_by_purchase_ownership_fence(monkeypatch):
    @contextmanager
    def guard(action, **_kwargs):
        assert action == "host_transaction_mutation"
        yield

    monkeypatch.setattr(state_module, "external_write_guard", guard)
    monkeypatch.setattr(
        state_module,
        "require_purchase_mutation_allowed",
        lambda *_args, **_kwargs: pytest.fail("purchase ownership must not inspect sales"),
    )
    monkeypatch.setattr(
        state_module,
        "db_update_sale",
        lambda idx, data: idx == 0 and data == {"price": 10},
    )

    assert state_module.State().update_sale(0, {"price": 10}) is True


def test_manual_delete_surfaces_stable_auto_offer_managed_code(monkeypatch):
    def blocked(_db_id):
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_PURCHASE_MANAGED")

    monkeypatch.setattr(transaction_routes, "delete_purchase_by_id", blocked)
    result = transaction_routes.api_delete_transaction(type="purchase", db_id=7)

    assert result["ok"] is False
    assert result["code"] == "AUTO_OFFER_PURCHASE_MANAGED"
    assert "Auto Offer" in result["error"]


def test_manual_update_surfaces_stable_ownership_unsafe_code(monkeypatch):
    def blocked(_db_id, _data):
        raise HostPurchaseMutationBlockedError("AUTO_OFFER_OWNERSHIP_UNSAFE")

    monkeypatch.setattr(transaction_routes, "update_purchase_by_id", blocked)
    body = transaction_routes.TransactionUpdateBody(
        type="purchase",
        db_id=7,
        price=1.23,
    )
    result = transaction_routes.api_update_transaction(body)

    assert result["ok"] is False
    assert result["code"] == "AUTO_OFFER_OWNERSHIP_UNSAFE"
    assert "Auto Offer" in result["error"]


def test_manual_update_surfaces_delivery_identity_immutable_code(monkeypatch):
    def blocked(_db_id, _data):
        raise HostPurchaseMutationBlockedError(
            "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE"
        )

    monkeypatch.setattr(transaction_routes, "update_purchase_by_id", blocked)
    body = transaction_routes.TransactionUpdateBody(
        type="purchase",
        db_id=7,
        goods_id=999,
    )
    result = transaction_routes.api_update_transaction(body)

    assert result["ok"] is False
    assert result["code"] == "AUTO_OFFER_DELIVERY_IDENTITY_IMMUTABLE"
