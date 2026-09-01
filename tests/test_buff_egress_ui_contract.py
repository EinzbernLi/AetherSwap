from pathlib import Path


def test_settings_surface_exposes_buff_egress_selector_without_pool_strategy_binding():
    source = Path("web/js/proxy.js").read_text(encoding="utf-8")

    assert 'id="cfg-buff-egress-mode"' in source
    assert '<option value="direct">' in source
    assert '<option value="system_proxy">' in source
    assert "cfg.buff.egress_mode" in source
    assert "此设置不使用代理池轮换" in source


def test_authenticated_buff_transport_does_not_import_generic_proxy_manager():
    egress_source = Path("app/services/buff_egress.py").read_text(encoding="utf-8")
    client_source = Path("app/services/buff_client.py").read_text(encoding="utf-8")
    auth_source = Path("app/services/buff_auth.py").read_text(encoding="utf-8")

    for source in (egress_source, client_source, auth_source):
        assert "utils.proxy_manager" not in source
        assert "get_proxy_manager" not in source
