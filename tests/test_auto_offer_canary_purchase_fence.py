import time

import pytest

from app import pipeline_steps as steps
from app.auto_offer.canary_purchase_fence import CanarySinglePurchaseBuffClient


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
