import base64
import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional
_CONFIG_DIR = Path(__file__).resolve().parent
_CREDENTIALS_FILE = _CONFIG_DIR / "credentials.json"
_cache: dict = {}
_credentials_lock = threading.RLock()
_STEAM_IDENTITY_SECRET_BYTES = 20
def _normalize_steam_identity_secret(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(character.isspace() for character in value)
    ):
        raise ValueError("steam_identity_secret_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("steam_identity_secret_invalid") from None
    if len(decoded) != _STEAM_IDENTITY_SECRET_BYTES:
        raise ValueError("steam_identity_secret_invalid")
    return value
def _validated_identity_secret_or_none(value: object) -> Optional[str]:
    if value is None or value == "":
        return None
    return _normalize_steam_identity_secret(value)
def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON durably without exposing readers to a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
def _load() -> dict:
    global _cache
    with _credentials_lock:
        if _cache:
            return _cache
        if _CREDENTIALS_FILE.exists():
            try:
                with open(_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                _cache = loaded if isinstance(loaded, dict) else {}
            except Exception:
                _cache = {}
        else:
            _cache = {}
        return _cache
def get(section: str, key: str = None, default: Any = None) -> Any:
    with _credentials_lock:
        data = _load()
        val = data.get(section, default if key is None else {})
        if key is not None:
            val = val.get(key, default) if isinstance(val, dict) else default
        return copy.deepcopy(val)
def get_steam() -> dict:
    with _credentials_lock:
        steam = copy.deepcopy(_load().get("steam", {}))
        if isinstance(steam, dict) and steam.get("identity_secret") not in (None, ""):
            steam["identity_secret"] = _normalize_steam_identity_secret(steam["identity_secret"])
        return steam
def get_buff() -> dict:
    with _credentials_lock:
        return copy.deepcopy(_load().get("buff", {}))
def get_all_credentials() -> dict:
    with _credentials_lock:
        data = copy.deepcopy(_load())
        steam = data.get("steam")
        if isinstance(steam, dict) and steam.get("identity_secret") not in (None, ""):
            steam["identity_secret"] = _normalize_steam_identity_secret(steam["identity_secret"])
        return data
def save_credentials(data: dict) -> None:
    """Replace credentials while assigning imported BUFF data a local revision."""
    global _cache
    with _credentials_lock:
        current = _load()
        saved = copy.deepcopy(data if isinstance(data, dict) else {})
        incoming_steam = saved.get("steam")
        if isinstance(incoming_steam, dict) and "identity_secret" in incoming_steam:
            secret = _validated_identity_secret_or_none(incoming_steam.get("identity_secret"))
            if secret is None:
                incoming_steam.pop("identity_secret", None)
            else:
                incoming_steam["identity_secret"] = secret
        incoming_buff = saved.get("buff")
        if isinstance(incoming_buff, dict):
            try:
                current_generation = int((current.get("buff") or {}).get("generation", 0))
            except (TypeError, ValueError):
                current_generation = 0
            incoming_buff["generation"] = current_generation + 1
        _atomic_write_json(_CREDENTIALS_FILE, saved)
        _cache = {}
_STEAM_COOKIE_NAMES = ("sessionid", "steamCountry", "steamLoginSecure")
def _filter_steam_cookies(cookie_str: str) -> str:
    seen = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, val = part.partition("=")
            n = name.strip()
            if n in _STEAM_COOKIE_NAMES:
                seen[n] = val.strip()
    return "; ".join(f"{k}={seen[k]}" for k in _STEAM_COOKIE_NAMES if k in seen)
def _steam_id_from_cookies(cookies: str) -> Optional[str]:
    for part in (cookies or "").split(";"):
        part = part.strip()
        if part.lower().startswith("steamloginsecure="):
            val = part.split("=", 1)[1].strip()
            if "%7C%7C" in val:
                return val.split("%7C%7C")[0].strip()
            if "||" in val:
                return val.split("||")[0].strip()
            if val.isdigit():
                return val
    return None
def update_steam_credentials(cookies: str, session_id: str, steam_id: str = None) -> None:
    global _cache
    with _credentials_lock:
        data = _load().copy()
        steam = dict(data.get("steam", {}))
        if steam.get("identity_secret") not in (None, ""):
            steam["identity_secret"] = _normalize_steam_identity_secret(steam["identity_secret"])
        steam["cookies"] = _filter_steam_cookies(cookies)
        steam["session_id"] = session_id
        sid = steam_id or _steam_id_from_cookies(cookies)
        if sid:
            steam["steam_id"] = sid
        data["steam"] = steam
        _atomic_write_json(_CREDENTIALS_FILE, data)
        _cache = {}
def update_buff_credentials(
    cookies: str,
    user_agent: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Persist BUFF credentials and advance their observable generation.

    ``user_agent`` and ``source`` are optional so older callers that only pass a
    cookie string remain compatible.  Omitting either field preserves the last
    known value instead of silently changing the browser identity.
    """
    global _cache
    with _credentials_lock:
        data = _load().copy()
        buff = dict(data.get("buff", {}))
        buff["cookies"] = cookies
        if user_agent is not None and user_agent.strip():
            buff["user_agent"] = user_agent.strip()
        if source is not None and source.strip():
            buff["source"] = source.strip().lower()
        try:
            generation = int(buff.get("generation", 0)) + 1
        except (TypeError, ValueError):
            generation = 1
        buff["generation"] = generation
        data["buff"] = buff
        _atomic_write_json(_CREDENTIALS_FILE, data)
        _cache = {}
_APP_CONFIG_FILE = _CONFIG_DIR / "app_config.json"
_app_config_lock = threading.RLock()
def get_app_config_path() -> Path:
    return _APP_CONFIG_FILE
def _read_app_config_unlocked() -> dict:
    if _APP_CONFIG_FILE.exists():
        try:
            with open(_APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            pass
    return {}
def _legacy_identity_secret(data: dict) -> tuple[bool, Optional[str]]:
    section = data.get("steam_confirm") if isinstance(data, dict) else None
    if not isinstance(section, dict) or "identity_secret" not in section:
        return False, None
    return True, _validated_identity_secret_or_none(section.get("identity_secret"))
def _scrub_legacy_identity_secret(data: dict) -> dict:
    scrubbed = copy.deepcopy(data if isinstance(data, dict) else {})
    section = scrubbed.get("steam_confirm")
    if isinstance(section, dict):
        section.pop("identity_secret", None)
    return scrubbed
def _migrate_legacy_identity_secret_locked(raw_app_config: dict) -> dict:
    """Move a legacy app-config secret to credentials without guessing."""
    global _cache
    had_legacy_field, legacy_secret = _legacy_identity_secret(raw_app_config)
    if not had_legacy_field:
        return raw_app_config
    if legacy_secret is not None:
        with _credentials_lock:
            credentials = _load().copy()
            steam = dict(credentials.get("steam", {}))
            canonical = _validated_identity_secret_or_none(steam.get("identity_secret"))
            if canonical is not None and canonical != legacy_secret:
                raise ValueError("steam_identity_secret_conflict")
            if canonical is None:
                steam["identity_secret"] = legacy_secret
                credentials["steam"] = steam
                _atomic_write_json(_CREDENTIALS_FILE, credentials)
                _cache = {}
    scrubbed = _scrub_legacy_identity_secret(raw_app_config)
    _atomic_write_json(_APP_CONFIG_FILE, scrubbed)
    return scrubbed
def load_app_config() -> dict:
    with _app_config_lock:
        return _migrate_legacy_identity_secret_locked(_read_app_config_unlocked())
def save_app_config(data: dict) -> None:
    """Persist app config while routing legacy-form secret input to credentials."""
    global _cache
    with _app_config_lock:
        saved = copy.deepcopy(data if isinstance(data, dict) else {})
        _had_field, candidate = _legacy_identity_secret(saved)
        if candidate is not None:
            with _credentials_lock:
                credentials = _load().copy()
                steam = dict(credentials.get("steam", {}))
                canonical = _validated_identity_secret_or_none(steam.get("identity_secret"))
                if canonical != candidate:
                    steam["identity_secret"] = candidate
                    credentials["steam"] = steam
                    _atomic_write_json(_CREDENTIALS_FILE, credentials)
                    _cache = {}
        _atomic_write_json(_APP_CONFIG_FILE, _scrub_legacy_identity_secret(saved))
