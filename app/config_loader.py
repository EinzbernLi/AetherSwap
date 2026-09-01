import copy
import threading
import time as _time
from app.config_schema import DEFAULTS, _validate_ranges, merge, validate_and_fill
from config import (
    get_app_config_path,
    get_buff,
    get_steam,
    load_app_config,
    save_app_config,
    update_buff_credentials,
    update_steam_credentials,
)
_config_cache: dict = {}
_config_cache_ts: float = 0.0
_config_cache_revision = None
_CONFIG_CACHE_TTL = 5.0
_CONFIG_SNAPSHOT_RETRIES = 3
_AUTH_BUFF_SOURCES = frozenset({"playwright", "playwright_ephemeral", "manual"})
_config_cache_lock = threading.Lock()
_config_update_lock = threading.RLock()
def _config_file_revision():
    try:
        stat = get_app_config_path().stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except OSError:
        return None
def _load_stable_app_config():
    raw = {}
    revision = None
    for _ in range(_CONFIG_SNAPSHOT_RETRIES):
        before = _config_file_revision()
        raw = load_app_config()
        revision = _config_file_revision()
        if before == revision:
            return raw, revision, True
    return raw, revision, False
def _invalidate_config_cache() -> None:
    global _config_cache, _config_cache_ts, _config_cache_revision
    with _config_cache_lock:
        _config_cache = {}
        _config_cache_ts = 0.0
        _config_cache_revision = None
def _with_runtime_steam_identity_secret(config: dict) -> dict:
    """Derive the legacy runtime alias from the canonical credential owner."""
    result = copy.deepcopy(config)
    steam = get_steam()
    secret = steam.get("identity_secret") if isinstance(steam, dict) else None
    section = result.get("steam_confirm")
    if not isinstance(section, dict):
        section = {}
        result["steam_confirm"] = section
    section["identity_secret"] = secret if type(secret) is str else ""
    return result
def get_steam_credentials() -> dict:
    load_app_config()
    return get_steam()
def get_buff_credentials() -> dict:
    return get_buff()
def update_steam_creds(cookies: str, session_id: str, steam_id: str = None) -> None:
    update_steam_credentials(cookies, session_id, steam_id)
def update_buff_creds(
    cookies: str,
    user_agent: str = None,
    source: str = None,
    egress_mode: str = None,
    egress_fingerprint: str = None,
) -> None:
    normalized_source = str(source or "").strip().lower()
    if (
        normalized_source in _AUTH_BUFF_SOURCES
        and egress_mode is None
        and egress_fingerprint is None
    ):
        # Browser/manual verification has already resolved the exact route used
        # for the successful probe.  Persist that same route identity with the
        # credential generation rather than resolving ambient proxy state again.
        from app.services.buff_auth import get_prepared_buff_egress_binding

        binding = get_prepared_buff_egress_binding()
        if binding is None:
            raise ValueError("buff_egress_binding_not_prepared")
        egress_mode = binding.mode
        egress_fingerprint = binding.fingerprint
    update_buff_credentials(
        cookies,
        user_agent=user_agent,
        source=source,
        egress_mode=egress_mode,
        egress_fingerprint=egress_fingerprint,
    )
def get_buff_credentials_generation() -> int:
    """Return the credential revision used by long-lived BUFF clients."""
    try:
        return int((get_buff_credentials() or {}).get("generation", 0))
    except (TypeError, ValueError):
        return 0
def load_app_config_validated() -> dict:
    global _config_cache, _config_cache_ts, _config_cache_revision
    now = _time.monotonic()
    with _config_cache_lock:
        revision = _config_file_revision()
        if (
            _config_cache
            and revision == _config_cache_revision
            and (now - _config_cache_ts) < _CONFIG_CACHE_TTL
        ):
            return _with_runtime_steam_identity_secret(_config_cache)
        raw, revision, stable = _load_stable_app_config()
        result = _validate_ranges(validate_and_fill(merge(DEFAULTS, raw)))
        steam_confirm = result.get("steam_confirm")
        if isinstance(steam_confirm, dict):
            steam_confirm["identity_secret"] = ""
        if stable:
            _config_cache = result
            _config_cache_ts = now
            _config_cache_revision = revision
        else:
            # Never associate content from one file generation with the
            # revision of another. The next call will retry the disk read.
            _config_cache = {}
            _config_cache_ts = 0.0
            _config_cache_revision = None
        return _with_runtime_steam_identity_secret(result)
def save_app_config_validated(data: dict) -> None:
    with _config_update_lock:
        filled = _validate_ranges(validate_and_fill(merge(DEFAULTS, data)))
        save_app_config(filled)
        _invalidate_config_cache()


def update_app_config_validated(patch: dict) -> dict:
    """Atomically merge a partial update into the latest app configuration."""
    with _config_update_lock:
        current = load_app_config_validated()
        updated = merge(current, patch if isinstance(patch, dict) else {})
        save_app_config_validated(updated)
        return load_app_config_validated()
