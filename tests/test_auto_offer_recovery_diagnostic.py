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


def test_malformed_current_read_does_not_claim_history_fallback(monkeypatch):
    result, client, adapter = _run(
        monkeypatch,
        (PlatformResultStatus.MALFORMED, "malformed_payload"),
        (PlatformResultStatus.MALFORMED, "malformed_payload"),
    )
    assert result.current_status is PlatformResultStatus.MALFORMED
    assert result.final_detail == "malformed_payload"
    assert result.history_fallback_used is False
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
