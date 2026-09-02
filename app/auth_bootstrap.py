"""Narrow auth-only AetherSwap startup surface.

This module intentionally does not import or mount ``app.api``.  It reuses the
existing Steam relogin route implementation while exposing only the local
account/configuration operations needed to prepare an isolated deployment.
No business worker, pipeline, Auto Offer runtime, purchase/listing/inventory or
receipt surface is started here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.accounts import (
    add_account,
    get_current_account,
    get_current_id,
    list_accounts,
    set_current,
)
from app.config_loader import (
    get_steam_credentials,
    load_app_config_validated,
    update_app_config_validated,
)
from app.routes import auth as existing_auth


app = FastAPI(
    title="AetherSwap Steam Auth Preparation",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_HTML_PATH = Path(__file__).resolve().parents[1] / "web" / "auth_bootstrap.html"


class AccountCreateBody(BaseModel):
    label: str = ""


class AccountSelectBody(BaseModel):
    slot: int


class SteamPreparationConfigBody(BaseModel):
    shared_secret: Optional[str] = None
    identity_secret: Optional[str] = None
    device_id: Optional[str] = None
    confirmation_enabled: Optional[bool] = None


def _valid_steam_id(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or not value.isdecimal()
        or value[0] == "0"
    ):
        return False
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return number > 0 and str(number) == value


def _cookie_has(cookie_text: object, name: str) -> bool:
    if type(cookie_text) is not str:
        return False
    wanted = name.casefold()
    for part in cookie_text.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip().casefold() == wanted and value.strip():
            return True
    return False


def _accounts_payload() -> dict:
    accounts = list_accounts()
    current_id = get_current_id()
    current_slot = None
    slots = []
    for index, account in enumerate(accounts):
        is_current = account.get("id") == current_id
        if current_id is None and index == 0:
            # Existing account semantics treat the first account as current when
            # no explicit current_id has yet been persisted.
            is_current = True
        if is_current:
            current_slot = index
        slots.append(
            {
                "slot": index,
                "current": is_current,
                "steam_identity_present": _valid_steam_id(account.get("steam_id")),
            }
        )
    return {
        "ok": True,
        "account_count": len(accounts),
        "current_slot": current_slot,
        "slots": slots,
    }


def _readiness_payload() -> dict:
    """Return local-only, sanitized authentication readiness state.

    This function deliberately performs no Steam/BUFF/network probe and never
    returns account IDs, Steam IDs, cookies, passwords or Steam Guard secrets.
    """

    try:
        current = get_current_account()
    except Exception:
        current = None
    try:
        credentials = get_steam_credentials()
    except Exception:
        credentials = {}
    try:
        config = load_app_config_validated()
    except Exception:
        config = {}

    if not isinstance(current, dict):
        current = None
    if not isinstance(credentials, dict):
        credentials = {}
    if not isinstance(config, dict):
        config = {}

    registry_steam_id = current.get("steam_id") if current else None
    persisted_steam_id = credentials.get("steam_id")
    registry_valid = _valid_steam_id(registry_steam_id)
    persisted_valid = _valid_steam_id(persisted_steam_id)
    identities_match = bool(
        registry_valid and persisted_valid and registry_steam_id == persisted_steam_id
    )

    session_material_present = bool(
        type(credentials.get("session_id")) is str
        and credentials.get("session_id")
        and _cookie_has(credentials.get("cookies"), "steamLoginSecure")
    )

    steam_guard = config.get("steam_guard")
    if not isinstance(steam_guard, dict):
        steam_guard = {}
    steam_confirm = config.get("steam_confirm")
    if not isinstance(steam_confirm, dict):
        steam_confirm = {}

    shared_secret_present = bool(
        type(steam_guard.get("shared_secret")) is str
        and steam_guard.get("shared_secret").strip()
    )
    identity_secret_present = bool(
        type(steam_confirm.get("identity_secret")) is str
        and steam_confirm.get("identity_secret").strip()
    )
    device_id_present = bool(
        type(steam_confirm.get("device_id")) is str
        and steam_confirm.get("device_id").strip()
    )
    confirmation_enabled = steam_confirm.get("enabled") is True
    confirmation_ready = bool(identity_secret_present and device_id_present)

    authenticated_read_ready = bool(
        current is not None
        and registry_valid
        and persisted_valid
        and identities_match
        and session_material_present
    )
    ready = bool(
        authenticated_read_ready
        and (not confirmation_enabled or confirmation_ready)
    )

    return {
        "ok": True,
        "ready": ready,
        "authenticated_read_ready": authenticated_read_ready,
        "current_account_exists": current is not None,
        "registry_steam_identity_present": registry_valid,
        "persisted_session_material_present": session_material_present,
        "persisted_steam_identity_present": persisted_valid,
        "steam_identities_match": identities_match,
        "steam_guard_shared_secret_present": shared_secret_present,
        "confirmation_enabled": confirmation_enabled,
        "confirmation_identity_secret_present": identity_secret_present,
        "confirmation_device_id_present": device_id_present,
        "confirmation_ready": confirmation_ready,
    }


@app.get("/", response_class=HTMLResponse)
def auth_bootstrap_page():
    try:
        return _HTML_PATH.read_text(encoding="utf-8")
    except Exception:
        return HTMLResponse(
            "<h1>AetherSwap Steam Auth Preparation</h1>"
            "<p>Auth-only UI asset is unavailable.</p>",
            status_code=500,
        )


@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": "auth_only"}


@app.get("/api/auth-bootstrap/readiness")
def api_auth_bootstrap_readiness():
    return _readiness_payload()


@app.get("/api/auth-bootstrap/accounts")
def api_auth_bootstrap_accounts():
    return _accounts_payload()


@app.post("/api/auth-bootstrap/accounts/create")
def api_auth_bootstrap_account_create(body: AccountCreateBody):
    if list_accounts():
        return {"ok": False, "code": "account_already_exists"}
    label = " ".join((body.label or "").split())[:128]
    try:
        created = add_account(username=label)
        account_id = created.get("id") if isinstance(created, dict) else None
        if type(account_id) is not str or not account_id or not set_current(account_id):
            return {"ok": False, "code": "account_create_failed"}
    except Exception:
        return {"ok": False, "code": "account_create_failed"}
    return {"ok": True, "accounts": _accounts_payload(), "readiness": _readiness_payload()}


@app.post("/api/auth-bootstrap/accounts/select")
def api_auth_bootstrap_account_select(body: AccountSelectBody):
    accounts = list_accounts()
    if type(body.slot) is not int or body.slot < 0 or body.slot >= len(accounts):
        return {"ok": False, "code": "account_slot_invalid"}
    account_id = accounts[body.slot].get("id")
    if type(account_id) is not str or not set_current(account_id):
        return {"ok": False, "code": "account_select_failed"}
    return {"ok": True, "accounts": _accounts_payload(), "readiness": _readiness_payload()}


@app.post("/api/auth-bootstrap/steam/config")
def api_auth_bootstrap_steam_config(body: SteamPreparationConfigBody):
    patch: dict = {}
    fields_set = body.model_fields_set

    if "shared_secret" in fields_set:
        shared_secret = (body.shared_secret or "").strip()
        if not shared_secret or len(shared_secret) > 4096:
            return {"ok": False, "code": "steam_configuration_invalid"}
        patch["steam_guard"] = {"shared_secret": shared_secret}

    confirm_patch = {}
    if "identity_secret" in fields_set:
        identity_secret = (body.identity_secret or "").strip()
        if not identity_secret or len(identity_secret) > 4096:
            return {"ok": False, "code": "steam_configuration_invalid"}
        confirm_patch["identity_secret"] = identity_secret
    if "device_id" in fields_set:
        device_id = (body.device_id or "").strip()
        if not device_id or len(device_id) > 512:
            return {"ok": False, "code": "steam_configuration_invalid"}
        confirm_patch["device_id"] = device_id
    if "confirmation_enabled" in fields_set:
        confirm_patch["enabled"] = body.confirmation_enabled is True
    if confirm_patch:
        patch["steam_confirm"] = confirm_patch

    if not patch:
        return {"ok": True, "readiness": _readiness_payload()}
    try:
        update_app_config_validated(patch)
    except Exception:
        # Never echo secret input or underlying parser content into the response.
        return {"ok": False, "code": "steam_configuration_invalid"}
    return {"ok": True, "readiness": _readiness_payload()}


# Allowlisted wrappers over the existing Steam relogin implementation.  The
# generic BUFF endpoints and the rest of the normal app router are deliberately
# not mounted in auth-only mode.
@app.post("/api/auth/steam/relogin_start")
def api_auth_only_steam_relogin_start():
    return existing_auth.api_auth_steam_relogin_start()


@app.post("/api/auth/steam/relogin_finish")
def api_auth_only_steam_relogin_finish(body: existing_auth.ReloginFinishBody):
    return existing_auth.api_auth_steam_relogin_finish(body)


@app.post("/api/auth/steam/manual_cookie")
def api_auth_only_steam_manual_cookie(body: existing_auth.ManualCookieBody):
    return existing_auth.api_auth_manual_cookie("steam", body)
