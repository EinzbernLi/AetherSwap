import base64
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SECRET_A = base64.b64encode(b"A" * 20).decode("ascii")
_SECRET_B = base64.b64encode(b"B" * 20).decode("ascii")


@pytest.fixture
def isolated_identity_store(monkeypatch, tmp_path):
    import config
    from app import config_loader

    credentials_file = tmp_path / "credentials.json"
    app_config_file = tmp_path / "app_config.json"
    monkeypatch.setattr(config, "_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(config, "_APP_CONFIG_FILE", app_config_file)
    config._cache = {}
    config_loader._invalidate_config_cache()
    return config, config_loader, credentials_file, app_config_file


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_legacy_only_secret_migrates_credentials_first_and_scrubs_app_config(
    isolated_identity_store,
):
    config, loader, credentials_file, app_config_file = isolated_identity_store
    _write_json(credentials_file, {"steam": {"steam_id": "76561198000000001"}})
    _write_json(
        app_config_file,
        {
            "steam_confirm": {
                "enabled": True,
                "identity_secret": _SECRET_A,
                "device_id": "android:legacy",
            }
        },
    )

    steam = loader.get_steam_credentials()

    assert steam["identity_secret"] == _SECRET_A
    assert _read_json(credentials_file)["steam"]["identity_secret"] == _SECRET_A
    assert _read_json(app_config_file) == {
        "steam_confirm": {"enabled": True, "device_id": "android:legacy"}
    }


def test_equal_dual_secret_converges_to_credentials_only(isolated_identity_store):
    config, _loader, credentials_file, app_config_file = isolated_identity_store
    _write_json(credentials_file, {"steam": {"identity_secret": _SECRET_A}})
    _write_json(app_config_file, {"steam_confirm": {"identity_secret": _SECRET_A}})

    assert config.load_app_config() == {"steam_confirm": {}}
    assert _read_json(credentials_file)["steam"]["identity_secret"] == _SECRET_A
    assert _read_json(app_config_file) == {"steam_confirm": {}}


def test_conflicting_dual_secret_blocks_without_mutation(isolated_identity_store):
    config, _loader, credentials_file, app_config_file = isolated_identity_store
    credentials_before = {"steam": {"identity_secret": _SECRET_A}}
    app_before = {"steam_confirm": {"identity_secret": _SECRET_B}}
    _write_json(credentials_file, credentials_before)
    _write_json(app_config_file, app_before)

    with pytest.raises(ValueError, match="^steam_identity_secret_conflict$"):
        config.load_app_config()

    assert _read_json(credentials_file) == credentials_before
    assert _read_json(app_config_file) == app_before


def test_malformed_legacy_secret_fails_with_sanitized_error(isolated_identity_store):
    config, _loader, _credentials_file, app_config_file = isolated_identity_store
    secret = "not a valid secret value"
    _write_json(app_config_file, {"steam_confirm": {"identity_secret": secret}})

    with pytest.raises(ValueError) as exc_info:
        config.load_app_config()

    assert str(exc_info.value) == "steam_identity_secret_invalid"
    assert secret not in str(exc_info.value)


def test_save_credentials_rejects_invalid_canonical_secret_before_write(
    isolated_identity_store,
):
    config, _loader, credentials_file, _app_config_file = isolated_identity_store
    before = {"steam": {"steam_id": "76561198000000001"}}
    _write_json(credentials_file, before)

    with pytest.raises(ValueError, match="^steam_identity_secret_invalid$"):
        config.save_credentials({"steam": {"identity_secret": "invalid"}})

    assert _read_json(credentials_file) == before


def test_cookie_refresh_preserves_canonical_identity_secret(isolated_identity_store):
    config, _loader, credentials_file, _app_config_file = isolated_identity_store
    _write_json(
        credentials_file,
        {
            "steam": {
                "identity_secret": _SECRET_A,
                "cookies": "sessionid=old",
                "session_id": "old",
                "steam_id": "76561198000000001",
            }
        },
    )

    config.update_steam_credentials(
        "sessionid=new; steamCountry=CN; steamLoginSecure=76561198000000001||token",
        "new",
        "76561198000000001",
    )

    assert config.get_steam()["identity_secret"] == _SECRET_A


def test_explicit_app_config_secret_rotates_canonical_and_never_persists_in_app_config(
    isolated_identity_store,
):
    config, _loader, credentials_file, app_config_file = isolated_identity_store
    _write_json(credentials_file, {"steam": {"identity_secret": _SECRET_A}})

    config.save_app_config(
        {
            "steam_confirm": {
                "enabled": True,
                "identity_secret": _SECRET_B,
                "device_id": "android:legacy",
            }
        }
    )

    assert _read_json(credentials_file)["steam"]["identity_secret"] == _SECRET_B
    assert _read_json(app_config_file) == {
        "steam_confirm": {"enabled": True, "device_id": "android:legacy"}
    }


def test_empty_app_config_secret_preserves_existing_canonical(isolated_identity_store):
    config, _loader, credentials_file, app_config_file = isolated_identity_store
    _write_json(credentials_file, {"steam": {"identity_secret": _SECRET_A}})

    config.save_app_config(
        {"steam_confirm": {"enabled": True, "identity_secret": "", "device_id": "d"}}
    )

    assert _read_json(credentials_file)["steam"]["identity_secret"] == _SECRET_A
    assert "identity_secret" not in _read_json(app_config_file)["steam_confirm"]


def test_validated_runtime_config_derives_alias_from_credentials_without_disk_duplicate(
    isolated_identity_store,
):
    _config, loader, credentials_file, app_config_file = isolated_identity_store
    _write_json(credentials_file, {"steam": {"identity_secret": _SECRET_A}})
    _write_json(app_config_file, {"steam_confirm": {"enabled": True, "device_id": "d"}})

    loaded = loader.load_app_config_validated()

    assert loaded["steam_confirm"]["identity_secret"] == _SECRET_A
    assert "identity_secret" not in _read_json(app_config_file)["steam_confirm"]


def test_api_get_masks_identity_secret_and_never_returns_plaintext(monkeypatch):
    from fastapi import Response
    from app.routes import config as route

    monkeypatch.setattr(
        route,
        "load_app_config_validated",
        lambda: {"steam_confirm": {"enabled": True, "identity_secret": _SECRET_A}},
    )
    response = Response()

    result = route.api_get_config(response)

    assert result["config"]["steam_confirm"]["identity_secret"] == "********"
    assert _SECRET_A not in repr(result)
    assert response.headers["cache-control"] == "no-store"


def test_api_save_mask_means_preserve_and_is_not_forwarded(monkeypatch):
    from app.routes import config as route

    captured = {}

    def fake_update(patch):
        captured.update(copy.deepcopy(patch))
        return {"steam_confirm": {"enabled": True, "identity_secret": _SECRET_A}}

    monkeypatch.setattr(route, "update_app_config_validated", fake_update)

    result = route.api_save_config(
        route.ConfigBody(
            config={"steam_confirm": {"enabled": True, "identity_secret": "********"}}
        )
    )

    assert "identity_secret" not in captured["steam_confirm"]
    assert result["config"]["steam_confirm"]["identity_secret"] == "********"
    assert _SECRET_A not in repr(result)


def test_api_save_new_secret_is_forwarded_but_response_is_masked(monkeypatch):
    from app.routes import config as route

    captured = {}

    def fake_update(patch):
        captured.update(copy.deepcopy(patch))
        return {"steam_confirm": {"identity_secret": _SECRET_B}}

    monkeypatch.setattr(route, "update_app_config_validated", fake_update)

    result = route.api_save_config(
        route.ConfigBody(config={"steam_confirm": {"identity_secret": _SECRET_B}})
    )

    assert captured["steam_confirm"]["identity_secret"] == _SECRET_B
    assert result["config"]["steam_confirm"]["identity_secret"] == "********"
    assert _SECRET_B not in repr(result)


def test_full_import_preflight_rejects_conflicting_dual_owner_without_secret_echo():
    from app.routes import config as route

    with pytest.raises(RuntimeError) as exc_info:
        route._preflight_import_identity_secret(
            {"steam_confirm": {"identity_secret": _SECRET_A}},
            {"steam": {"identity_secret": _SECRET_B}},
        )

    assert str(exc_info.value) == "steam_identity_secret_conflict"
    assert _SECRET_A not in str(exc_info.value)
    assert _SECRET_B not in str(exc_info.value)


def test_full_import_preflight_allows_equal_or_single_source():
    from app.routes import config as route

    route._preflight_import_identity_secret(
        {"steam_confirm": {"identity_secret": _SECRET_A}},
        {"steam": {"identity_secret": _SECRET_A}},
    )
    route._preflight_import_identity_secret(
        {"steam_confirm": {"identity_secret": _SECRET_A}},
        None,
    )
    route._preflight_import_identity_secret(
        None,
        {"steam": {"identity_secret": _SECRET_A}},
    )
