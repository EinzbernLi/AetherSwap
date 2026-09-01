import json

import pytest


def _proxy_binding():
    from app.services.buff_egress import BuffEgressBinding, BUFF_EGRESS_SYSTEM_PROXY

    return BuffEgressBinding(
        mode=BUFF_EGRESS_SYSTEM_PROXY,
        fingerprint="a" * 64,
        _proxy_server="http://127.0.0.1:7890",
    )


def test_direct_binding_is_explicit_and_never_resolves_ambient_proxy():
    from app.services.buff_egress import resolve_buff_egress

    def forbidden_resolver():
        raise AssertionError("direct mode must not inspect ambient proxy")

    binding = resolve_buff_egress(
        {"buff": {"egress_mode": "direct"}},
        proxy_resolver=forbidden_resolver,
        bypass_resolver=lambda _host: False,
    )

    assert binding.mode == "direct"
    assert len(binding.fingerprint) == 64
    assert binding.requests_proxies() == {}
    assert "--no-proxy-server" in binding.browser_launch_args()
    assert not any(arg.startswith("--proxy-server=") for arg in binding.browser_launch_args())


def test_system_proxy_binding_is_identical_for_browser_and_requests():
    from app.services.buff_egress import configure_requests_session, resolve_buff_egress

    binding = resolve_buff_egress(
        {"buff": {"egress_mode": "system_proxy"}},
        proxy_resolver=lambda: {"https": "127.0.0.1:7890"},
        bypass_resolver=lambda _host: False,
    )

    assert binding.mode == "system_proxy"
    assert "--proxy-server=http://127.0.0.1:7890" in binding.browser_launch_args()
    assert "--no-proxy-server" not in binding.browser_launch_args()
    assert binding.requests_proxies() == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }

    class Session:
        def __init__(self):
            self.trust_env = True
            self.proxies = {"old": "ambient"}

    session = configure_requests_session(Session(), binding)
    assert session.trust_env is False
    assert session.proxies == binding.requests_proxies()


def test_system_proxy_normalizes_scheme_host_and_default_port():
    from app.services.buff_egress import resolve_buff_egress

    binding = resolve_buff_egress(
        {"buff": {"egress_mode": "system_proxy"}},
        proxy_resolver=lambda: {"https": "HTTPS://LOCALHOST"},
        bypass_resolver=lambda _host: False,
    )

    assert binding.requests_proxies()["https"] == "https://localhost:443"


@pytest.mark.parametrize(
    ("proxies", "bypass", "expected_code"),
    [
        ({}, False, "BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE"),
        ({"https": "socks5://127.0.0.1:7890"}, False, "BUFF_EGRESS_SYSTEM_PROXY_SCHEME_UNSUPPORTED"),
        ({"https": "http://user:secret@127.0.0.1:7890"}, False, "BUFF_EGRESS_SYSTEM_PROXY_AUTH_UNSUPPORTED"),
        ({"https": "http://127.0.0.1:7890/path"}, False, "BUFF_EGRESS_SYSTEM_PROXY_INVALID"),
        ({"https": "http://127.0.0.1:7890"}, True, "BUFF_EGRESS_SYSTEM_PROXY_BYPASS"),
    ],
)
def test_system_proxy_resolution_fails_closed_without_network(proxies, bypass, expected_code):
    from app.services.buff_egress import BuffEgressError, resolve_buff_egress

    with pytest.raises(BuffEgressError) as exc_info:
        resolve_buff_egress(
            {"buff": {"egress_mode": "system_proxy"}},
            proxy_resolver=lambda: proxies,
            bypass_resolver=lambda _host: bypass,
        )

    assert exc_info.value.code == expected_code
    assert "secret" not in str(exc_info.value)


def test_sanitized_status_never_returns_proxy_material():
    binding = _proxy_binding()
    status = binding.sanitized_status()

    assert status == {"mode": "system_proxy", "binding": "proxy"}
    assert "127.0.0.1" not in json.dumps(status)


def test_legacy_credentials_are_adopted_only_for_direct_mode():
    from app.services.buff_egress import (
        BuffEgressReauthRequired,
        direct_buff_egress_binding,
        validate_buff_credential_binding,
    )

    assert validate_buff_credential_binding(
        {"cookies": "session=legacy"}, direct_buff_egress_binding()
    ) == "legacy_direct"

    with pytest.raises(BuffEgressReauthRequired) as exc_info:
        validate_buff_credential_binding({"cookies": "session=legacy"}, _proxy_binding())
    assert exc_info.value.code == "BUFF_EGRESS_REAUTH_REQUIRED"


def test_bound_credentials_require_exact_mode_and_fingerprint():
    from app.services.buff_egress import (
        BuffEgressReauthRequired,
        validate_buff_credential_binding,
    )

    binding = _proxy_binding()
    credentials = {
        "cookies": "session=one",
        "egress_mode": binding.mode,
        "egress_fingerprint": binding.fingerprint.upper(),
    }
    assert validate_buff_credential_binding(credentials, binding) == "bound"

    credentials["egress_fingerprint"] = "b" * 64
    with pytest.raises(BuffEgressReauthRequired) as exc_info:
        validate_buff_credential_binding(credentials, binding)
    assert exc_info.value.code == "BUFF_EGRESS_REAUTH_REQUIRED"


def test_buff_credential_refresh_persists_binding_once_and_cookie_rotation_preserves_it(
    monkeypatch,
    tmp_path,
):
    import config as credential_config

    credentials_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credential_config, "_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(credential_config, "_cache", {})

    binding = _proxy_binding()
    credential_config.update_buff_credentials(
        "session=one",
        user_agent="Browser/1.0",
        source="playwright",
        egress_mode=binding.mode,
        egress_fingerprint=binding.fingerprint,
    )
    first = json.loads(credentials_file.read_text(encoding="utf-8"))["buff"]
    assert first["generation"] == 1
    assert first["egress_mode"] == "system_proxy"
    assert first["egress_fingerprint"] == binding.fingerprint

    credential_config.update_buff_credentials("session=two")
    second = json.loads(credentials_file.read_text(encoding="utf-8"))["buff"]
    assert second["generation"] == 2
    assert second["egress_mode"] == first["egress_mode"]
    assert second["egress_fingerprint"] == first["egress_fingerprint"]


def test_bulk_import_rejects_partial_or_malformed_egress_metadata(monkeypatch, tmp_path):
    import config as credential_config

    monkeypatch.setattr(credential_config, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credential_config, "_cache", {})

    with pytest.raises(ValueError, match="buff_egress_binding_invalid"):
        credential_config.save_credentials(
            {"buff": {"cookies": "session=x", "egress_mode": "system_proxy"}}
        )


def test_config_schema_defaults_direct_and_normalizes_unknown_mode():
    from app.config_schema import DEFAULTS, _validate_ranges

    assert DEFAULTS["buff"]["egress_mode"] == "direct"
    cfg = {"buff": {"egress_mode": "SYSTEM_PROXY"}}
    assert _validate_ranges(cfg)["buff"]["egress_mode"] == "system_proxy"
    bad = {"buff": {"egress_mode": "rotate_randomly"}}
    assert _validate_ranges(bad)["buff"]["egress_mode"] == "direct"


def test_auth_refresh_wrapper_commits_the_already_prepared_binding(monkeypatch):
    from app import config_loader
    from app.services import buff_auth

    binding = _proxy_binding()
    captured = {}
    monkeypatch.setattr(buff_auth, "_prepared_buff_egress_binding", binding)
    monkeypatch.setattr(
        config_loader,
        "update_buff_credentials",
        lambda cookies, **kwargs: captured.update({"cookies": cookies, **kwargs}),
    )

    config_loader.update_buff_creds(
        "session=new",
        user_agent="Browser/2.0",
        source="manual",
    )

    assert captured["egress_mode"] == binding.mode
    assert captured["egress_fingerprint"] == binding.fingerprint
    assert captured["cookies"] == "session=new"


def test_prepare_browser_binding_updates_stable_launch_argument_list(monkeypatch):
    from app.services import buff_auth

    binding = _proxy_binding()
    original_list = buff_auth.BUFF_BROWSER_LAUNCH_ARGS
    monkeypatch.setattr(buff_auth, "resolve_buff_egress", lambda _cfg: binding)

    prepared = buff_auth.prepare_buff_egress_binding({"buff": {"egress_mode": "system_proxy"}})

    assert prepared is binding
    assert buff_auth.BUFF_BROWSER_LAUNCH_ARGS is original_list
    assert "--proxy-server=http://127.0.0.1:7890" in original_list
    assert "--no-proxy-server" not in original_list


def test_browser_preflight_blocks_on_egress_resolution_before_policy(monkeypatch):
    from app.services import buff_auth
    from app.services.buff_egress import BuffEgressError

    monkeypatch.setattr(
        buff_auth,
        "prepare_buff_egress_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BuffEgressError("BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE")
        ),
    )

    allowed, status, reason = buff_auth.browser_buff_verification_allowed()
    assert allowed is False
    assert status == "egress_unavailable"
    assert reason == "BUFF_EGRESS_SYSTEM_PROXY_UNAVAILABLE"


def test_buff_client_provider_drift_blocks_before_buyer_operation(monkeypatch):
    from app.services import buff_client
    from app.services.buff_egress import BuffEgressReauthRequired, direct_buff_egress_binding

    calls = []

    class Buyer:
        def __init__(self, *_args, **_kwargs):
            calls.append("construct")

        def verify_session(self, *_args, **_kwargs):
            calls.append("verify")
            return True

        def close(self):
            calls.append("close")

    monkeypatch.setattr(buff_client, "BuffBuyer", Buyer)
    direct = direct_buff_egress_binding()
    client = buff_client.BuffClient(
        "session=legacy",
        egress_binding=direct,
        egress_binding_provider=lambda: _proxy_binding(),
    )

    with pytest.raises(BuffEgressReauthRequired) as exc_info:
        client.verify_session()
    assert exc_info.value.code == "BUFF_EGRESS_REAUTH_REQUIRED"
    assert "verify" not in calls
    client.close()


def test_buff_client_bound_proxy_session_is_explicit_and_ambient_disabled(monkeypatch):
    from app.services import buff_client

    captured = {}

    class Buyer:
        def __init__(self, _cookies, **kwargs):
            captured["session"] = kwargs["session"]

        def close(self):
            return None

    monkeypatch.setattr(buff_client, "BuffBuyer", Buyer)
    binding = _proxy_binding()
    client = buff_client.BuffClient("session=x", egress_binding=binding)

    session = captured["session"]
    assert session.trust_env is False
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    client.close()


def test_create_client_blocks_legacy_credentials_in_proxy_mode_before_buyer_construction(monkeypatch):
    from app.services import buff_client
    from app.services.buff_egress import BuffEgressReauthRequired

    monkeypatch.setattr(buff_client, "resolve_buff_egress", lambda _cfg: _proxy_binding())
    monkeypatch.setattr(
        buff_client,
        "BuffBuyer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("buyer must not be constructed before egress validation")
        ),
    )

    with pytest.raises(BuffEgressReauthRequired):
        buff_client.create_buff_client_from_config(
            {"cookies": "session=legacy", "generation": 10},
            {"buff": {"egress_mode": "system_proxy"}},
        )
