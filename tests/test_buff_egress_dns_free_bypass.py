import socket

import pytest


def _raise_network_use(*_args, **_kwargs):
    raise AssertionError("BUFF egress resolution must remain local-only")


def test_default_system_proxy_resolution_uses_one_snapshot_without_dns(monkeypatch):
    from app.services import buff_egress
    import requests.utils

    calls = []

    def snapshot():
        calls.append("getproxies")
        return {
            "https": "127.0.0.1:7890",
            "no": "localhost;*.internal.example",
        }

    monkeypatch.setattr(buff_egress, "getproxies", snapshot)
    monkeypatch.setattr(requests.utils, "proxy_bypass", _raise_network_use)
    monkeypatch.setattr(socket, "gethostbyname", _raise_network_use)
    monkeypatch.setattr(socket, "getfqdn", _raise_network_use)

    binding = buff_egress.resolve_buff_egress(
        {"buff": {"egress_mode": "system_proxy"}}
    )

    assert calls == ["getproxies"]
    assert binding.mode == "system_proxy"
    assert binding.requests_proxies() == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.parametrize(
    "bypass_value",
    [
        "buff.163.com",
        ".163.com",
        "163.com",
        "*.163.com",
        "other.example;buff.163.com:443",
        "other.example,buff.163.com",
        "*",
    ],
)
def test_snapshot_bypass_rules_still_fail_closed(bypass_value):
    from app.services import buff_egress

    with pytest.raises(buff_egress.BuffEgressError) as exc_info:
        buff_egress.resolve_buff_egress(
            {"buff": {"egress_mode": "system_proxy"}},
            proxy_resolver=lambda: {
                "https": "127.0.0.1:7890",
                "no": bypass_value,
            },
        )

    assert exc_info.value.code == "BUFF_EGRESS_SYSTEM_PROXY_BYPASS"


def test_local_and_unrelated_snapshot_bypass_entries_do_not_bypass_buff():
    from app.services import buff_egress

    binding = buff_egress.resolve_buff_egress(
        {"buff": {"egress_mode": "system_proxy"}},
        proxy_resolver=lambda: {
            "https": "127.0.0.1:7890",
            "no": "<local>;localhost;example.com;*.internal.example",
        },
    )

    assert binding.mode == "system_proxy"
    assert binding.requests_proxies()["https"] == "http://127.0.0.1:7890"


def test_no_proxy_alias_is_supported_by_snapshot_bypass_parser():
    from app.services import buff_egress

    with pytest.raises(buff_egress.BuffEgressError) as exc_info:
        buff_egress.resolve_buff_egress(
            {"buff": {"egress_mode": "system_proxy"}},
            proxy_resolver=lambda: {
                "https": "127.0.0.1:7890",
                "no_proxy": "buff.163.com",
            },
        )

    assert exc_info.value.code == "BUFF_EGRESS_SYSTEM_PROXY_BYPASS"


def test_explicit_injected_bypass_resolver_remains_supported():
    from app.services import buff_egress

    calls = []

    def injected(host):
        calls.append(host)
        return True

    with pytest.raises(buff_egress.BuffEgressError) as exc_info:
        buff_egress.resolve_buff_egress(
            {"buff": {"egress_mode": "system_proxy"}},
            proxy_resolver=lambda: {"https": "127.0.0.1:7890"},
            bypass_resolver=injected,
        )

    assert calls == [buff_egress.BUFF_HOST]
    assert exc_info.value.code == "BUFF_EGRESS_SYSTEM_PROXY_BYPASS"


def test_production_module_has_no_secondary_proxy_bypass_dependency():
    from app.services import buff_egress

    assert not hasattr(buff_egress, "proxy_bypass")
    assert not hasattr(buff_egress, "_default_proxy_bypass")
    assert callable(buff_egress._proxy_snapshot_bypasses_host)
