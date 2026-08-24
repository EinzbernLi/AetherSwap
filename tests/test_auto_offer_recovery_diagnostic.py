from types import SimpleNamespace

import pytest

import app.auto_offer.recovery_diagnostic as diagnostic
from app.auto_offer.adapters import (
    OfferStateEvidence,
    PlatformResult,
    PlatformResultStatus,
)


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


class _Client:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Adapter:
    def __init__(self, current, final):
        self.current = current
        self.final = final
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return PlatformResult(
            request=request,
            status=self.current[0],
            detail=self.current[1],
        )

    def _recover_result_unknown_offer_state(self, request, current):
        status, detail = self.final
        evidence = None
        if status is PlatformResultStatus.SUCCESS:
            evidence = OfferStateEvidence("123456", "76561198000000001")
        return PlatformResult(
            request=request,
            status=status,
            detail=detail,
            evidence=evidence,
        )


def _run(monkeypatch, current, final):
    binding = _binding()
    client = _Client()
    adapter = _Adapter(current, final)
    monkeypatch.setattr(diagnostic, "_make_buff_client", lambda _binding: client)
    monkeypatch.setattr(
        diagnostic,
        "BuffReadOnlyAdapter",
        lambda _client, *, account_id: adapter,
    )
    result = diagnostic.diagnose_buff_read(binding)
    return result, client, adapter


def _history_payload(items, *, page_num=1, page_size=10, total_page=1):
    return {
        "code": "OK",
        "data": {
            "page_num": page_num,
            "page_size": page_size,
            "total_page": total_page,
            "items": items,
        },
    }


def _exact_item(**overrides):
    item = {
        "id": "order-1",
        "buyer_steamid": "76561198000000000",
        "tradeofferid": "123456",
        "seller_steam_id": "76561198000000001",
    }
    item.update(overrides)
    return item


def test_malformed_current_read_does_not_claim_history_fallback(monkeypatch):
    result, client, adapter = _run(
        monkeypatch,
        (PlatformResultStatus.MALFORMED, "malformed_payload"),
        (PlatformResultStatus.MALFORMED, "malformed_payload"),
    )
    assert result.current_status is PlatformResultStatus.MALFORMED
    assert result.final_detail == "malformed_payload"
    assert result.history_fallback_used is False
    assert result.history_schema_trace == ()
    assert result.history_tradeoffer_trace == ()
    assert len(adapter.requests) == 1
    assert client.closed is True


def test_result_unknown_current_read_reports_history_fallback(monkeypatch):
    result, client, _adapter = _run(
        monkeypatch,
        (PlatformResultStatus.RESULT_UNKNOWN, "order_not_proven"),
        (PlatformResultStatus.SUCCESS, "offer_history_recovered"),
    )
    assert result.current_detail == "order_not_proven"
    assert result.final_status is PlatformResultStatus.SUCCESS
    assert result.final_detail == "offer_history_recovered"
    assert result.history_fallback_used is True
    assert result.history_tradeoffer_trace == ()
    assert client.closed is True


def test_auth_failure_does_not_claim_history_fallback(monkeypatch):
    result, _client, _adapter = _run(
        monkeypatch,
        (PlatformResultStatus.FAILURE, "auth_failed"),
        (PlatformResultStatus.FAILURE, "auth_failed"),
    )
    assert result.final_status is PlatformResultStatus.FAILURE
    assert result.final_detail == "auth_failed"
    assert result.history_fallback_used is False


def test_history_schema_trace_accepts_exact_shape_without_raw_values():
    code = diagnostic._classify_history_payload(
        _history_payload([_exact_item()]),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:target_fields_shape_valid"
    assert "order-1" not in code
    assert "765611" not in code


def test_history_schema_trace_pinpoints_page_envelope_mismatch():
    code = diagnostic._classify_history_payload(
        _history_payload([], page_size="10"),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:page_size_mismatch_or_type"


def test_history_schema_trace_pinpoints_unrelated_invalid_item_id():
    code = diagnostic._classify_history_payload(
        _history_payload([{"id": None}, _exact_item()]),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:item_id_invalid"


def test_history_schema_trace_pinpoints_tradeoffer_alias_conflict():
    code = diagnostic._classify_history_payload(
        _history_payload([_exact_item(trade_offer_id="654321")]),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:target_tradeoffer_alias_invalid"


def test_history_schema_trace_pinpoints_seller_field_invalidity():
    code = diagnostic._classify_history_payload(
        _history_payload([_exact_item(seller_steam_id=76561198000000001)]),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:target_seller_alias_invalid"


def test_history_schema_trace_distinguishes_missing_target():
    code = diagnostic._classify_history_payload(
        _history_payload([{"id": "other-order"}]),
        expected_page_num=1,
        target_order_id="order-1",
        recipient_steam_id="76561198000000000",
    )
    assert code == "p1:valid_no_target"


def test_tradeoffer_shape_single_canonical_alias_is_sanitized():
    code = diagnostic._tradeoffer_alias_shape(_exact_item(), page_num=1)
    assert code == (
        "p1:tradeofferid=string_canonical|trade_offer_id=absent|relation=single"
    )
    assert "123456" not in code


def test_tradeoffer_shape_null_secondary_alias_is_invalid_without_value_leak():
    code = diagnostic._tradeoffer_alias_shape(
        _exact_item(trade_offer_id=None), page_num=1
    )
    assert code == (
        "p1:tradeofferid=string_canonical|trade_offer_id=null|relation=invalid"
    )
    assert "123456" not in code


def test_tradeoffer_shape_conflicting_canonical_aliases_is_conflict():
    code = diagnostic._tradeoffer_alias_shape(
        _exact_item(trade_offer_id="654321"), page_num=1
    )
    assert code == (
        "p1:tradeofferid=string_canonical|trade_offer_id=string_canonical|relation=conflict"
    )
    assert "123456" not in code
    assert "654321" not in code


def test_tradeoffer_shape_equal_canonical_aliases_is_equal():
    code = diagnostic._tradeoffer_alias_shape(
        _exact_item(trade_offer_id="123456"), page_num=1
    )
    assert code == (
        "p1:tradeofferid=string_canonical|trade_offer_id=string_canonical|relation=equal"
    )


def test_tradeoffer_shape_classifies_noncanonical_string_and_other_types():
    item = _exact_item(tradeofferid=" 123456", trade_offer_id={"id": "hidden"})
    code = diagnostic._tradeoffer_alias_shape(item, page_num=1)
    assert code == (
        "p1:tradeofferid=string_noncanonical|trade_offer_id=mapping|relation=invalid"
    )
    assert "hidden" not in code


def test_tradeoffer_shape_from_history_only_emits_for_exact_target():
    absent = diagnostic._tradeoffer_shape_from_history_payload(
        _history_payload([{"id": "other-order"}]),
        expected_page_num=1,
        target_order_id="order-1",
    )
    exact = diagnostic._tradeoffer_shape_from_history_payload(
        _history_payload([_exact_item(trade_offer_id=None)]),
        expected_page_num=1,
        target_order_id="order-1",
    )
    assert absent is None
    assert exact == (
        "p1:tradeofferid=string_canonical|trade_offer_id=null|relation=invalid"
    )


def test_tracing_client_records_only_sanitized_schema_and_tradeoffer_codes():
    class Client:
        def get_steam_trades(self):
            return []

        def get_buy_order_history_page(self, page_num, game="csgo"):
            return _history_payload(
                [_exact_item(trade_offer_id=None)], page_num=page_num
            )

    traced = diagnostic._TracingBuffClient(Client(), _binding())
    payload = traced.get_buy_order_history_page(1, "csgo")
    assert payload["code"] == "OK"
    assert traced.history_schema_trace == ["p1:target_tradeoffer_alias_invalid"]
    assert traced.history_tradeoffer_trace == [
        "p1:tradeofferid=string_canonical|trade_offer_id=null|relation=invalid"
    ]


def test_main_fingerprint_mismatch_stops_before_live_diagnostic(monkeypatch, capsys):
    binding = _binding()
    monkeypatch.setattr(
        diagnostic,
        "collect_recovery_preflight",
        lambda **_kwargs: binding,
    )
    monkeypatch.setattr(
        diagnostic,
        "diagnose_buff_read",
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
        "TASK049_BUFF_DIAGNOSTIC_BLOCKED reason=target_fingerprint_mismatch"
    )
