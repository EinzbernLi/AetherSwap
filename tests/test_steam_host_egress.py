import pytest

from app.services import steam_egress


class FakeProxyManager:
    def __init__(self, result=None, *, error=None, always_proxy=False):
        self.result = result
        self.error = error
        self.always_proxy = always_proxy
        self.calls = []

    def get_proxies_for_request(self, failed=False):
        self.calls.append(failed)
        if self.error is not None:
            raise self.error
        return self.result

    def should_always_use_proxy(self):
        return self.always_proxy


class FakeSession:
    def __init__(self, *, verify=True, fail_get=None, fail_post=None):
        self.verify = verify
        self.trust_env = True
        self.fail_get = fail_get
        self.fail_post = fail_post
        self.calls = []
        self.closed = 0

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if self.fail_get is not None:
            raise self.fail_get
        return object()

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if self.fail_post is not None:
            raise self.fail_post
        return object()

    def close(self):
        self.closed += 1


@pytest.mark.parametrize(
    ("strategy", "route"),
    [
        (1, None),
        (2, {"http": "proxy-route", "https": "proxy-route"}),
        (3, None),
    ],
)
def test_each_route_policy_performs_one_underlying_call(strategy, route):
    manager = FakeProxyManager(route, always_proxy=strategy == 2)
    session = FakeSession()
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    facade.get(
        "https://example.invalid/exact",
        params={"a": "b"},
        headers={"X-Test": "1"},
        cookies={"sessionid": "opaque"},
        timeout=(5.0, 15.0),
        allow_redirects=False,
    )

    assert manager.calls == [False]
    assert session.calls == [
        (
            "get",
            "https://example.invalid/exact",
            {
                "params": {"a": "b"},
                "headers": {"X-Test": "1"},
                "cookies": {"sessionid": "opaque"},
                "timeout": (5.0, 15.0),
                "allow_redirects": False,
                "proxies": route,
            },
        )
    ]


def test_proxy_only_policy_without_route_fails_before_http():
    manager = FakeProxyManager(None, always_proxy=True)
    session = FakeSession()
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    with pytest.raises(
        steam_egress.SteamHostEgressError,
        match="steam_egress_required_proxy_unavailable",
    ):
        facade.get(
            "https://example.invalid/exact",
            timeout=(5.0, 15.0),
            allow_redirects=False,
        )

    assert manager.calls == [False]
    assert session.calls == []


def test_post_preserves_caller_kwargs_and_injects_only_source_route():
    route = {"http": "proxy-route", "https": "proxy-route"}
    manager = FakeProxyManager(route, always_proxy=True)
    session = FakeSession()
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    facade.post(
        "https://example.invalid/write",
        data={"x": "y"},
        headers={"Referer": "exact"},
        cookies={"sessionid": "opaque"},
        timeout=(7.0, 7.0),
        allow_redirects=False,
    )

    assert manager.calls == [False]
    assert session.calls == [
        (
            "post",
            "https://example.invalid/write",
            {
                "data": {"x": "y"},
                "headers": {"Referer": "exact"},
                "cookies": {"sessionid": "opaque"},
                "timeout": (7.0, 7.0),
                "allow_redirects": False,
                "proxies": route,
            },
        )
    ]


def test_request_exception_never_selects_or_attempts_a_second_route():
    manager = FakeProxyManager(None)
    session = FakeSession(fail_get=TimeoutError("boom"))
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    with pytest.raises(TimeoutError, match="boom"):
        facade.get(
            "https://example.invalid/exact",
            timeout=(5.0, 15.0),
            allow_redirects=False,
        )

    assert manager.calls == [False]
    assert len(session.calls) == 1


def test_route_selection_failure_happens_before_any_http_attempt():
    manager = FakeProxyManager(error=RuntimeError("no route"))
    session = FakeSession()
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    with pytest.raises(
        steam_egress.SteamHostEgressError,
        match="steam_egress_route_selection_failed",
    ):
        facade.get("https://example.invalid/exact", allow_redirects=False)

    assert manager.calls == [False]
    assert session.calls == []


def test_caller_cannot_override_source_owned_proxies():
    manager = FakeProxyManager(None)
    session = FakeSession()
    facade = steam_egress.SteamHostEgressSession(
        session=session,
        proxy_manager=manager,
    )

    with pytest.raises(
        steam_egress.SteamHostEgressError,
        match="steam_egress_proxies_forbidden",
    ):
        facade.get(
            "https://example.invalid/exact",
            proxies={"https": "forbidden"},
        )

    assert manager.calls == []
    assert session.calls == []


def test_verify_false_underlying_session_is_rejected_before_use():
    session = FakeSession(verify=False)

    with pytest.raises(
        steam_egress.SteamHostEgressError,
        match="steam_egress_tls_verification_disabled",
    ):
        steam_egress.SteamHostEgressSession(
            session=session,
            proxy_manager=FakeProxyManager(None),
        )

    assert session.calls == []


def test_internal_session_disables_environment_proxy_inheritance(monkeypatch):
    created = FakeSession()
    monkeypatch.setattr(steam_egress.requests, "Session", lambda: created)

    facade = steam_egress.SteamHostEgressSession(
        proxy_manager=FakeProxyManager(None),
    )

    assert created.trust_env is False
    assert facade.verify is True
    facade.close()
    facade.close()
    assert created.closed == 1
