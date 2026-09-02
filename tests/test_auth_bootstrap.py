from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app.auth_bootstrap as bootstrap


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_auth_bootstrap_import_does_not_import_normal_runtime():
    code = r'''
import sys
import app.auth_bootstrap
for forbidden in (
    "app.api",
    "app.database",
    "app.services.workers",
    "app.pipeline",
    "app.auto_offer.host_integration",
):
    assert forbidden not in sys.modules, forbidden
print("AUTH_BOOTSTRAP_IMPORT_FENCE_OK")
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUTH_BOOTSTRAP_IMPORT_FENCE_OK" in result.stdout


def test_auth_bootstrap_exposes_only_allowlisted_routes():
    paths = {route.path for route in bootstrap.app.routes}
    assert paths == {
        "/",
        "/healthz",
        "/api/auth-bootstrap/readiness",
        "/api/auth-bootstrap/accounts",
        "/api/auth-bootstrap/accounts/create",
        "/api/auth-bootstrap/accounts/select",
        "/api/auth-bootstrap/steam/config",
        "/api/auth/steam/relogin_start",
        "/api/auth/steam/relogin_finish",
        "/api/auth/steam/manual_cookie",
    }
    assert not any("/buff/" in path for path in paths)
    assert not any("pipeline" in path for path in paths)
    assert not any("auto_offer" in path for path in paths)


def test_browser_relogin_delegates_to_existing_auth(monkeypatch):
    calls = []
    expected = {"ok": True, "delegated": "start"}

    def fake_start():
        calls.append("start")
        return expected

    monkeypatch.setattr(bootstrap.existing_auth, "api_auth_steam_relogin_start", fake_start)

    assert bootstrap.api_auth_only_steam_relogin_start() is expected
    assert calls == ["start"]


def test_finish_and_manual_cookie_delegate_to_existing_auth(monkeypatch):
    calls = []

    def fake_finish(body):
        calls.append(("finish", body.success))
        return {"ok": True}

    def fake_manual(kind, body):
        calls.append(("manual", kind, body.cookies, body.session_id, body.steam_id))
        return {"ok": True}

    monkeypatch.setattr(bootstrap.existing_auth, "api_auth_steam_relogin_finish", fake_finish)
    monkeypatch.setattr(bootstrap.existing_auth, "api_auth_manual_cookie", fake_manual)

    finish = bootstrap.existing_auth.ReloginFinishBody(success=True)
    manual = bootstrap.existing_auth.ManualCookieBody(
        cookies="steamLoginSecure=fake",
        session_id="session",
        steam_id="76561198000000001",
    )

    assert bootstrap.api_auth_only_steam_relogin_finish(finish) == {"ok": True}
    assert bootstrap.api_auth_only_steam_manual_cookie(manual) == {"ok": True}
    assert calls == [
        ("finish", True),
        (
            "manual",
            "steam",
            "steamLoginSecure=fake",
            "session",
            "76561198000000001",
        ),
    ]


def test_readiness_is_local_and_never_returns_sensitive_values(monkeypatch):
    registry_id = "76561198000000001"
    local_account_id = "local-secret-id"
    cookie_secret = "steamLoginSecure=76561198000000001%7C%7CSECRET; sessionid=SESSIONSECRET"
    identity_secret = "IDENTITY-SECRET-SHOULD-NOT-LEAK"
    shared_secret = "SHARED-SECRET-SHOULD-NOT-LEAK"

    monkeypatch.setattr(
        bootstrap,
        "get_current_account",
        lambda: {
            "id": local_account_id,
            "steam_id": registry_id,
            "username": "private-user",
            "password": "private-password",
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "get_steam_credentials",
        lambda: {
            "steam_id": registry_id,
            "cookies": cookie_secret,
            "session_id": "SESSIONSECRET",
            "identity_secret": identity_secret,
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "load_app_config_validated",
        lambda: {
            "steam_guard": {"shared_secret": shared_secret},
            "steam_confirm": {
                "enabled": True,
                "identity_secret": identity_secret,
                "device_id": "android:private-device",
            },
        },
    )

    # Any accidental HTTP call from readiness is a test failure.
    import requests

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("network call")),
    )

    payload = bootstrap._readiness_payload()
    rendered = repr(payload)

    assert payload["ready"] is True
    assert payload["authenticated_read_ready"] is True
    assert payload["steam_identities_match"] is True
    assert payload["confirmation_ready"] is True
    for secret in (
        registry_id,
        local_account_id,
        "SECRET",
        "SESSIONSECRET",
        identity_secret,
        shared_secret,
        "android:private-device",
        "private-user",
        "private-password",
    ):
        assert secret not in rendered


def test_readiness_fails_closed_on_identity_mismatch(monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "get_current_account",
        lambda: {"id": "local", "steam_id": "76561198000000001"},
    )
    monkeypatch.setattr(
        bootstrap,
        "get_steam_credentials",
        lambda: {
            "steam_id": "76561198000000002",
            "cookies": "steamLoginSecure=x",
            "session_id": "session",
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "load_app_config_validated",
        lambda: {"steam_guard": {}, "steam_confirm": {"enabled": False}},
    )

    payload = bootstrap._readiness_payload()
    assert payload["steam_identities_match"] is False
    assert payload["authenticated_read_ready"] is False
    assert payload["ready"] is False


def test_account_creation_is_first_account_only_and_does_not_sync_region(monkeypatch):
    calls = []
    accounts = []

    monkeypatch.setattr(bootstrap, "list_accounts", lambda: list(accounts))
    monkeypatch.setattr(
        bootstrap,
        "add_account",
        lambda username="": (
            accounts.append({"id": "local-1", "username": username, "steam_id": ""})
            or accounts[0]
        ),
    )
    monkeypatch.setattr(bootstrap, "set_current", lambda account_id: calls.append(account_id) or True)
    monkeypatch.setattr(bootstrap, "_accounts_payload", lambda: {"ok": True, "account_count": len(accounts)})
    monkeypatch.setattr(bootstrap, "_readiness_payload", lambda: {"ok": True, "ready": False})

    created = bootstrap.api_auth_bootstrap_account_create(
        bootstrap.AccountCreateBody(label=" isolated   account ")
    )
    duplicate = bootstrap.api_auth_bootstrap_account_create(
        bootstrap.AccountCreateBody(label="second")
    )

    assert created["ok"] is True
    assert accounts[0]["username"] == "isolated account"
    assert calls == ["local-1"]
    assert duplicate == {"ok": False, "code": "account_already_exists"}


def test_account_selection_uses_sanitized_slot_not_public_account_id(monkeypatch):
    accounts = [
        {"id": "private-a", "steam_id": ""},
        {"id": "private-b", "steam_id": "76561198000000001"},
    ]
    selected = []
    monkeypatch.setattr(bootstrap, "list_accounts", lambda: list(accounts))
    monkeypatch.setattr(bootstrap, "set_current", lambda account_id: selected.append(account_id) or True)
    monkeypatch.setattr(bootstrap, "_accounts_payload", lambda: {"ok": True, "account_count": 2})
    monkeypatch.setattr(bootstrap, "_readiness_payload", lambda: {"ok": True, "ready": False})

    result = bootstrap.api_auth_bootstrap_account_select(bootstrap.AccountSelectBody(slot=1))

    assert result["ok"] is True
    assert selected == ["private-b"]
    assert "private-b" not in repr(result)


def test_steam_config_delegates_validated_persistence_and_never_echoes_secrets(monkeypatch):
    patches = []
    secret = "very-private-secret"
    identity = "very-private-identity"
    device = "android:very-private-device"

    monkeypatch.setattr(
        bootstrap,
        "update_app_config_validated",
        lambda patch: patches.append(patch) or {},
    )
    monkeypatch.setattr(bootstrap, "_readiness_payload", lambda: {"ok": True, "ready": False})

    body = bootstrap.SteamPreparationConfigBody(
        shared_secret=secret,
        identity_secret=identity,
        device_id=device,
        confirmation_enabled=True,
    )
    result = bootstrap.api_auth_bootstrap_steam_config(body)

    assert result == {"ok": True, "readiness": {"ok": True, "ready": False}}
    assert patches == [
        {
            "steam_guard": {"shared_secret": secret},
            "steam_confirm": {
                "identity_secret": identity,
                "device_id": device,
                "enabled": True,
            },
        }
    ]
    assert secret not in repr(result)
    assert identity not in repr(result)
    assert device not in repr(result)


def test_steam_config_parser_errors_do_not_echo_secret(monkeypatch):
    secret = "invalid-private-secret"

    def reject(_patch):
        raise ValueError(f"bad secret {secret}")

    monkeypatch.setattr(bootstrap, "update_app_config_validated", reject)

    result = bootstrap.api_auth_bootstrap_steam_config(
        bootstrap.SteamPreparationConfigBody(identity_secret=secret)
    )

    assert result == {"ok": False, "code": "steam_configuration_invalid"}
    assert secret not in repr(result)


def test_launcher_is_loopback_and_never_targets_normal_app():
    source = (REPO_ROOT / "run_auth.py").read_text(encoding="utf-8")
    assert '"app.auth_bootstrap:app"' in source
    assert 'host="127.0.0.1"' in source
    assert "app.api" not in source
    assert "reload=False" in source
