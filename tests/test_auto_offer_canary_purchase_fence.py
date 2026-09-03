import time
from contextlib import nullcontext

import pytest

from app import pipeline as pipeline_module
from app import pipeline_steps as steps
from app.auto_offer.canary_purchase_fence import CanarySinglePurchaseBuffClient
from app.auto_offer.canary_takeover import (
    CanaryTakeoverIntegration,
    CanaryTakeoverPhase,
)
from app.auto_offer.contracts import AutoOfferResult


@pytest.fixture(autouse=True)
def _isolated_checkout_guard(monkeypatch, tmp_path):
    from app.services import buff_checkout_guard

    monkeypatch.setattr(
        buff_checkout_guard,
        "_GUARD_PATH",
        tmp_path / "buff_checkout_guard.json",
    )


def _config():
    return {
        "buff": {
            "game": "csgo",
            "pay_method": "wechat",
            "price_tolerance": 0.5,
        },
        "pipeline": {"buff_sell_orders_cache_ttl_seconds": 3},
        "_strategy_runtime": {"buy": {"enabled_modules": []}},
    }


def _item():
    return {
        "name": "Canary Batch Candidate",
        "steam_market_name": "Canary Batch Candidate",
        "goods_id": 123,
        "min_price": 10.0,
        "daily_volume": 100,
        "_buff_lowest_price": 10.0,
        "_buff_sell_orders": [
            {"id": "sell-1", "price": "10.0"},
            {"id": "sell-2", "price": "10.0"},
        ],
        "_buff_sell_orders_fetched_at": time.time(),
    }


def test_canary_client_forces_real_checkout_path_to_one_committed_purchase(monkeypatch):
    class BuffClient:
        _pay_method = "wechat"
        supports_batch_buy = True

        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def verify_session(self, _game):
            return True

        def try_batch_buy(self, *_args, **_kwargs):
            self.batch_calls += 1
            raise AssertionError("prepared canary must not send a batch purchase")

        def lock_and_get_pay_url(self, _game, _goods_id, order_id, _price):
            self.single_calls += 1
            return {
                "success": True,
                "order_id": f"bill-{order_id}",
                "pay_url": "https://pay.invalid/canary-single",
                "pay_type": "wechat",
            }

    delegate = BuffClient()
    client = CanarySinglePurchaseBuffClient(delegate)
    pending = []
    purchases = []
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)

    paid = steps.lock_and_confirm_payment(
        client,
        _item(),
        _config(),
        target_balance=100.0,
        acc=0.0,
        set_pending_payment=pending.append,
        wait_payment_confirm=lambda **_kwargs: True,
        confirm_payment=lambda _ok: None,
        is_stop_requested=lambda: False,
        append_purchase=purchases.append,
    )

    assert paid == 10.0
    assert delegate.batch_calls == 0
    assert delegate.single_calls == 1
    assert len(purchases) == 1
    assert purchases[0]["buff_order_id"] == "bill-sell-1"
    assert pending[-1] is None


def test_prepared_canary_pipeline_callsite_uses_single_purchase_client(monkeypatch):
    class Controller:
        purchase_blocked = False
        owner_active = False
        phase = CanaryTakeoverPhase.PREPARED

    class NormalIntegration:
        def next_purchase_result(self, _purchases):
            return AutoOfferResult.WAITING

    class State:
        def get_purchases(self):
            return []

        def set_pending_payment(self, _value):
            return None

        def wait_payment_confirm(self, **_kwargs):
            return False

        def confirm_payment(self, _value):
            return None

        def is_stop_requested(self):
            return False

        def append_purchase(self, _purchase):
            raise AssertionError("checkout stub must not commit")

    class Context:
        def __init__(self):
            self.state = State()
            self.verbose = False

        def is_stop_requested(self):
            return False

        def log(self, *_args, **_kwargs):
            return None

        def set_status(self, *_args, **_kwargs):
            return None

        def debug(self, *_args, **_kwargs):
            return None

    seen = []

    def checkout(client, *_args, **_kwargs):
        seen.append(client)
        return steps.TIME_WINDOW_CLOSED

    monkeypatch.setattr(
        pipeline_module,
        "pick_stable_item",
        lambda *_args, **_kwargs: (_item(), set()),
    )
    monkeypatch.setattr(pipeline_module, "lock_and_confirm_payment", checkout)
    monkeypatch.setattr(
        pipeline_module,
        "external_write_guard",
        lambda _operation: nullcontext(),
    )

    wrapper = CanaryTakeoverIntegration(Controller(), NormalIntegration())
    result = pipeline_module._process_deals_for_target_impl(
        Context(),
        [_item()],
        _config(),
        100.0,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
        auto_offer_integration=wrapper,
        effective_cfg=_config(),
    )

    assert result[2] is steps.TIME_WINDOW_CLOSED
    assert len(seen) == 1
    assert isinstance(seen[0], CanarySinglePurchaseBuffClient)
