from __future__ import annotations

import threading

import pytest

from app import pipeline
from app.services import buff_checkout_guard as guard


def _history_page(page_num=1, total_page=1, items=()):
    return {
        "code": "OK",
        "data": {
            "page_num": page_num,
            "page_size": 10,
            "total_page": total_page,
            "items": list(items),
        },
    }


def _payment_failed(order_id="order-1"):
    return {"id": order_id, "state": "FAIL", "state_text": "支付失败"}


def _unresolved_guard(order_id="order-1", stage="order_created_pending"):
    intent = guard.begin_checkout("single", 123)
    guard.update_checkout(
        expected_intent_id=intent["intent_id"],
        stage=stage,
        order_id=order_id,
    )
    return intent


class _HistoryClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_buy_order_history_page(self, page_num, game):
        self.calls.append((page_num, game))
        return self.pages.get(page_num)


@pytest.fixture(autouse=True)
def _isolated_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_GUARD_PATH", tmp_path / "checkout.json")


def test_exact_payment_failed_row_resolves_only_same_order():
    _unresolved_guard("order-1")
    client = _HistoryClient({1: _history_page(items=[_payment_failed()])})

    assert guard.reconcile_order_created_pending(client) is True
    assert client.calls == [(1, "csgo")]
    assert guard.get_unresolved_checkout() is None


def test_exact_payment_failed_row_on_later_bounded_page_resolves():
    _unresolved_guard("order-1")
    client = _HistoryClient(
        {
            1: _history_page(page_num=1, total_page=2),
            2: _history_page(page_num=2, total_page=2, items=[_payment_failed()]),
        }
    )

    assert guard.reconcile_order_created_pending(client) is True
    assert client.calls == [(1, "csgo"), (2, "csgo")]
    assert guard.get_unresolved_checkout() is None


@pytest.mark.parametrize(
    "row",
    [
        {"id": "order-1", "state": "PAYING", "state_text": "等待付款"},
        {"id": "order-1", "state": "SUCCESS", "state_text": "购买成功"},
        {"id": "order-1", "state": "FAIL", "state_text": "购买失败-已退款"},
    ],
)
def test_other_exact_lifecycle_rows_remain_unresolved(row):
    _unresolved_guard("order-1")
    client = _HistoryClient({1: _history_page(items=[row])})

    assert guard.reconcile_order_created_pending(client) is False
    assert guard.get_unresolved_checkout()["unresolved"] is True


@pytest.mark.parametrize(
    "pages",
    [
        {1: _history_page(items=[{"id": "other-order", "state": "FAIL", "state_text": "支付失败"}])},
        {1: _history_page(items=[_payment_failed(), _payment_failed()])},
        {1: {"code": "OK", "data": {"page_num": 1, "page_size": 9, "total_page": 1, "items": []}}},
    ],
)
def test_missing_duplicate_or_malformed_identity_fails_closed(pages):
    _unresolved_guard("order-1")
    client = _HistoryClient(pages)

    assert guard.reconcile_order_created_pending(client) is False
    assert guard.get_unresolved_checkout()["unresolved"] is True


def test_non_reconcilable_stage_never_reads_history():
    _unresolved_guard("order-1", stage="write_result_unknown")
    client = _HistoryClient({1: _history_page(items=[_payment_failed()])})

    assert guard.reconcile_order_created_pending(client) is False
    assert client.calls == []
    assert guard.get_unresolved_checkout()["unresolved"] is True


def test_history_reader_failure_and_later_pages_are_bounded():
    _unresolved_guard("order-1")
    client = _HistoryClient(
        {
            1: _history_page(page_num=1, total_page=5),
            2: _history_page(page_num=2, total_page=5),
            3: _history_page(page_num=3, total_page=5),
        }
    )

    assert guard.reconcile_order_created_pending(client) is False
    assert client.calls == [(1, "csgo"), (2, "csgo"), (3, "csgo")]
    assert guard.get_unresolved_checkout()["unresolved"] is True


def test_start_pipeline_auto_reconciles_exact_failure_before_gate(monkeypatch):
    state = type(
        "State",
        (),
        {
            "get_status": lambda self: {
                "status": "idle",
                "step": "",
                "buff_auth_expired": False,
                "buff_verification_required": False,
            },
            "set_status": lambda self, *_args, **_kwargs: None,
        },
    )()
    _unresolved_guard("order-1")
    client = _HistoryClient({1: _history_page(items=[_payment_failed()])})
    started = threading.Event()

    class CredentialsClient:
        def get_buy_order_history_page(self, page_num, game):
            return client.get_buy_order_history_page(page_num, game)

        def close(self):
            pass

    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "get_buff_credentials", lambda: {"cookies": "present"})
    assert pipeline.get_pipeline_start_blocker()["code"] == "BUFF_RECONCILIATION_REQUIRED"
    monkeypatch.setattr(
        pipeline,
        "create_buff_client_from_config",
        lambda *_args: CredentialsClient(),
    )
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})
    monkeypatch.setattr(pipeline, "get_pipeline_runtime_blocker", lambda *_args: {})
    monkeypatch.setattr(pipeline, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(pipeline, "_snapshot_pipeline_start_config", lambda config: config)
    monkeypatch.setattr(pipeline, "_run_pipeline_guarded", lambda *_args: started.set())

    assert pipeline.start_pipeline({}) is True
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)
    assert started.wait(timeout=2) is True
    assert client.calls == [(1, "csgo")]
    assert guard.get_unresolved_checkout() is None


def test_pipeline_route_allows_start_to_attempt_reconciliation(monkeypatch):
    from app.routes import pipeline as pipeline_route

    _unresolved_guard("order-1")
    client = _HistoryClient({1: _history_page(items=[_payment_failed()])})
    started = []

    monkeypatch.setattr(
        pipeline_route,
        "get_pipeline_start_blocker",
        lambda: {"code": "BUFF_RECONCILIATION_REQUIRED", "message": "blocked"},
    )
    monkeypatch.setattr(
        pipeline_route,
        "start_pipeline",
        lambda *_args, **_kwargs: started.append(True) or True,
    )

    result = pipeline_route.api_pipeline_start(
        pipeline_route.ConfigBody(config={})
    )

    assert result == {"ok": True}
    assert started == [True]
