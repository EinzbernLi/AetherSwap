import pytest


def test_default_system_proxy_resolution_uses_requests_dns_free_bypass(monkeypatch):
    from app.services import buff_egress
    import requests.utils

    calls = []

    def dns_free_bypass(host):
        calls.append(host)
        return False

    monkeypatch.setattr(requests.utils, "proxy_bypass", dns_free_bypass)

    binding = buff_egress.resolve_buff_egress(
        {"buff": {"egress_mode": "system_proxy"}},
        proxy_resolver=lambda: {"https": "127.0.0.1:7890"},
    )

    assert calls == [buff_egress.BUFF_HOST]
    assert binding.mode == "system_proxy"
    assert binding.requests_proxies() == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_default_system_proxy_bypass_still_fails_closed(monkeypatch):
    from app.services import buff_egress
    import requests.utils

    monkeypatch.setattr(requests.utils, "proxy_bypass", lambda _host: True)

    with pytest.raises(buff_egress.BuffEgressError) as exc_info:
        buff_egress.resolve_buff_egress(
            {"buff": {"egress_mode": "system_proxy"}},
            proxy_resolver=lambda: {"https": "127.0.0.1:7890"},
        )

    assert exc_info.value.code == "BUFF_EGRESS_SYSTEM_PROXY_BYPASS"


def test_production_module_no_longer_holds_urllib_dns_capable_bypass_alias():
    from app.services import buff_egress

    # The prior production module imported urllib.request.proxy_bypass directly.
    # On Windows its Registry path may perform DNS resolution while evaluating
    # ProxyOverride.  The production default must now go through the explicit
    # DNS-free compatibility seam instead.
    assert not hasattr(buff_egress, "proxy_bypass")
    assert callable(buff_egress._default_proxy_bypass)
