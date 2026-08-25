from types import SimpleNamespace

import app.auto_offer.recovery_bill_order_facade_diagnostic as diagnostic


def _binding():
    snapshot = SimpleNamespace(
        purchase_id="buff:order-1",
        buff_order_id="order-1",
        account_id="account-1",
        recipient_steam_id="76561198000000000",
    )
    return SimpleNamespace(
        account_id="account-1",
        fingerprint="a" * 64,
        store=SimpleNamespace(revision=4, snapshot=snapshot),
    )


class _Buyer:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _make_request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.payload


class _Client:
    def __init__(self, payload):
        self.buyer = _Buyer(payload)
        self.run_calls = 0
        self.closed = False

    def _run(self, operation):
        self.run_calls += 1
        return operation(self.buyer)

    def close(self):
        self.closed = True


def _record(**changes):
    value = {
        "id": "order-1",
        "tradeofferid": "9001",
        "seller_steamid": "76561198000000001",
        "items_to_trade": [{"goods_id": 123, "assetid": "asset-secret"}],
        "asset_info": {"assetid": "asset-secret", "goods_id": 123},
        "trade_offer_url": "https://steamcommunity.com/tradeoffer/9001/?token=secret",
    }
    value.update(changes)
    return value


def test_exact_bill_order_get_runs_once_through_facade_and_is_sanitized(monkeypatch):
    client = _Client({"code": "OK", "data": {"order-1": _record()}})
    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: client)

    result = diagnostic.diagnose_bill_order(_binding())

    assert client.run_calls == 1
    assert len(client.buyer.calls) == 1
    method, url, kwargs = client.buyer.calls[0]
    assert method == "GET"
    assert url == diagnostic._API_BILL_ORDER_BATCH_INFO
    assert kwargs["params"] == {"bill_orders": "order-1"}
    assert client.closed is True
    assert result.binding_trace == "exact_target=data_key"
    combined = " ".join(
        (
            result.binding_trace,
            result.trade_trace,
            result.seller_trace,
            result.items_trace,
            result.asset_info_trace,
            result.trade_offer_url_trace,
        )
    )
    for secret in (
        "order-1",
        "9001",
        "765611",
        "asset-secret",
        "token=secret",
    ):
        assert secret not in combined


def test_missing_facade_runner_fails_before_buyer_request(monkeypatch):
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: client)

    try:
        diagnostic.diagnose_bill_order(_binding())
    except diagnostic.RecoveryCommandError as exc:
        assert str(exc) == "buff_facade_read_runner_unavailable"
    else:
        raise AssertionError("expected fail closed")


def test_missing_buyer_request_method_fails_closed(monkeypatch):
    class Client:
        def _run(self, operation):
            return operation(SimpleNamespace())

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: Client())

    try:
        diagnostic.diagnose_bill_order(_binding())
    except diagnostic.RecoveryCommandError as exc:
        assert str(exc) == "buff_buyer_read_method_unavailable"
    else:
        raise AssertionError("expected fail closed")


def test_non_ok_payload_fails_closed(monkeypatch):
    client = _Client({"code": "NOPE", "data": {}})
    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: client)

    try:
        diagnostic.diagnose_bill_order(_binding())
    except diagnostic.RecoveryCommandError as exc:
        assert str(exc) == "bill_order_non_ok"
    else:
        raise AssertionError("expected fail closed")


def test_main_fingerprint_mismatch_stops_before_network(monkeypatch, capsys):
    binding = _binding()
    monkeypatch.setattr(
        diagnostic,
        "collect_recovery_preflight",
        lambda **_kwargs: binding,
    )
    monkeypatch.setattr(
        diagnostic,
        "diagnose_bill_order",
        lambda _binding: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    code = diagnostic.main(
        [
            "--expected-commit",
            "c" * 40,
            "--expected-tree",
            "d" * 40,
            "--expected-fingerprint",
            "b" * 64,
        ]
    )

    assert code == 2
    assert capsys.readouterr().out.strip() == (
        "TASK049_BUFF_BILL_ORDER_FACADE_DIAGNOSTIC_BLOCKED "
        "reason=target_fingerprint_mismatch"
    )
