import pytest


def test_buff_page_expired_is_verification_required():
    from buff.buyer import _is_verification_required

    assert _is_verification_required({"code": "FAIL", "msg": "页面已过期，请刷新当前页面"})


def _fake_verification_response():
    class FakeResponse:
        status_code = 200
        text = '{"code":"FAIL","msg":"页面已过期，请刷新当前页面"}'

        def json(self):
            return {"code": "FAIL", "msg": "页面已过期，请刷新当前页面"}

    return FakeResponse()


def test_safe_read_verification_raises_verification_required_without_retry(monkeypatch):
    from buff.buyer import BuffBuyer, BuffVerificationRequired
    from buff.request_policy import BuffRequestPolicy

    calls = 0

    def fake_request(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return _fake_verification_response()

    monkeypatch.setattr("requests.Session.request", fake_request)
    buyer = BuffBuyer(
        "csrf_token=abc",
        request_policy=BuffRequestPolicy(min_interval=0, persist=False),
    )

    with pytest.raises(BuffVerificationRequired):
        buyer._make_request("GET", "https://buff.163.com/api/fake")

    assert calls == 1


def test_write_verification_raises_result_unknown_without_retry(monkeypatch):
    from buff.buyer import BuffBuyer
    from buff.request_policy import BuffRequestPolicy, BuffWriteResultUnknown

    calls = 0

    def fake_request(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return _fake_verification_response()

    monkeypatch.setattr("requests.Session.request", fake_request)
    buyer = BuffBuyer(
        "csrf_token=abc",
        request_policy=BuffRequestPolicy(min_interval=0, persist=False),
    )

    with pytest.raises(BuffWriteResultUnknown):
        buyer._make_request("POST", "https://buff.163.com/api/fake")

    assert calls == 1