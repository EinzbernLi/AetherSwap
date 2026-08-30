from __future__ import annotations

from app.services import buff_checkout_guard as guard


def _history_page(items):
    return {
        "code": "OK",
        "data": {
            "page_num": 1,
            "page_size": 10,
            "total_page": 1,
            "items": list(items),
        },
    }


def _payment_failed(order_id):
    return {"id": order_id, "state": "FAIL", "state_text": "支付失败"}


def _refunded(order_id):
    return {
        "id": order_id,
        "state": "FAIL",
        "state_text": "购买失败-已退款",
        "pay_expire_timeout": -1,
        "deliver_expire_timeout": -1,
        "receive_expire_timeout": -1,
        "buyer_send_offer_timeout": -1,
        "tradeofferid": None,
        "trade_offer_url": None,
    }


class _HistoryClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_buy_order_history_page(self, page_num, game):
        self.calls.append((page_num, game))
        return _history_page(self.items)


def test_multiple_terminal_rows_resolve_only_exact_guard_order(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_GUARD_PATH", tmp_path / "checkout.json")

    intent = guard.begin_checkout("single", 123)
    guard.update_checkout(
        expected_intent_id=intent["intent_id"],
        stage="order_created_pending",
        order_id="target-order",
    )
    client = _HistoryClient(
        [
            _payment_failed("other-payment-failure"),
            _refunded("target-order"),
            _refunded("other-refund"),
        ]
    )

    assert guard.reconcile_order_created_pending(client) is True
    assert client.calls == [(1, "csgo")]
    assert guard.get_unresolved_checkout() is None
