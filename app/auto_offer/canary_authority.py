"""Single-target live-canary authority and current-version write fencing.

This module owns no delivery state. It owns one user-scoped OS lock, a
secret-free one-shot generation record, an opaque Host-owned process capability,
and the final Host DB exclusion used immediately before Auto Offer SEND/CONFIRM.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_AUTHORITY_DIR_NAME = ".aetherswap"
_LOCK_FILE_NAME = "live-canary.lock"
_METADATA_FILE_NAME = "live-canary.json"
_METADATA_VERSION = 2
_MAX_USED_PERMIT_IDS = 128
_PRODUCTION_HOST_DB_PATH = Path(__file__).resolve().parents[2] / "config" / "app.db"
_ACTIVE_PHASES = frozenset({"armed", "completed"})
_ALLOWED_OWNER_ACTIONS = frozenset({"auto_offer_send", "auto_offer_confirm", "host_receipt"})
_DENIED_DURING_CANARY_ACTIONS = frozenset({
    "buff_purchase",
    "host_transaction_mutation",
    "legacy_receive",
    "sell_listing",
    "steam_delist",
    "legacy_bulk_confirm",
    "steam_gift_cart",
    "steam_gift_checkout",
})
_ALL_ACTIONS = _ALLOWED_OWNER_ACTIONS | _DENIED_DURING_CANARY_ACTIONS


class CanaryAuthorityError(RuntimeError):
    """Base fail-closed authority error. Messages are fixed reason codes."""


class CanaryAuthorityBusyError(CanaryAuthorityError):
    """Another current-version writer owns or contends on the namespace."""


class CanaryAuthorityStaleError(CanaryAuthorityError):
    """Durable active metadata exists without this live owner."""


class CanaryWriteBlockedError(CanaryAuthorityError):
    """The requested write is not authorized by the current context."""


def _exact_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise CanaryAuthorityError(f"invalid_{field}")
    if any(ord(character) < 32 for character in value):
        raise CanaryAuthorityError(f"invalid_{field}")
    return value


def _exact_positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CanaryAuthorityError(f"invalid_{field}")
    return value


def _exact_nonnegative_float(value: object, *, field: str) -> float:
    if type(value) not in (int, float):
        raise CanaryAuthorityError(f"invalid_{field}")
    number = float(value)
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        raise CanaryAuthorityError(f"invalid_{field}")
    return number


def _exact_optional_text(value: object, *, field: str) -> str | None:
    return None if value is None else _exact_text(value, field=field)


def _exact_optional_revision(value: object) -> int | None:
    return None if value is None else _exact_positive_int(value, field="expected_store_revision")


@dataclass(frozen=True)
class CanaryPermit:
    """Immutable secret-free permit for exactly one Host Purchase/BUFF order."""
    permit_id: str
    owner_nonce: str
    host_db_id: int
    buff_order_id: str
    purchase_id: str
    account_id: str
    recipient_steam_id: str
    expected_host_order_ids: tuple[str, ...]
    expected_store_present: bool
    expected_store_revision: int | None
    expected_store_status: str | None
    expected_store_tradeoffer_id: str | None
    created_at: float

    def __post_init__(self) -> None:
        _exact_text(self.permit_id, field="permit_id")
        _exact_text(self.owner_nonce, field="owner_nonce")
        _exact_positive_int(self.host_db_id, field="host_db_id")
        order_id = _exact_text(self.buff_order_id, field="buff_order_id")
        if self.purchase_id != f"buff:{order_id}":
            raise CanaryAuthorityError("invalid_purchase_id")
        _exact_text(self.account_id, field="account_id")
        _exact_text(self.recipient_steam_id, field="recipient_steam_id")
        if type(self.expected_host_order_ids) is not tuple or self.expected_host_order_ids != (order_id,):
            raise CanaryAuthorityError("invalid_expected_host_order_ids")
        if type(self.expected_store_present) is not bool:
            raise CanaryAuthorityError("invalid_expected_store_present")
        revision = _exact_optional_revision(self.expected_store_revision)
        status = _exact_optional_text(self.expected_store_status, field="expected_store_status")
        tradeoffer_id = _exact_optional_text(
            self.expected_store_tradeoffer_id,
            field="expected_store_tradeoffer_id",
        )
        if not self.expected_store_present:
            if revision is not None or status is not None or tradeoffer_id is not None:
                raise CanaryAuthorityError("absent_store_expectation_has_state")
        elif revision is None or status is None:
            raise CanaryAuthorityError("present_store_expectation_missing_state")
        _exact_nonnegative_float(self.created_at, field="created_at")

    def metadata_fields(self) -> dict[str, object]:
        return {
            "permit_id": self.permit_id,
            "owner_nonce": self.owner_nonce,
            "host_db_id": self.host_db_id,
            "buff_order_id": self.buff_order_id,
            "purchase_id": self.purchase_id,
            "account_id": self.account_id,
            "recipient_steam_id": self.recipient_steam_id,
            "expected_host_order_ids": list(self.expected_host_order_ids),
            "expected_store_present": self.expected_store_present,
            "expected_store_revision": self.expected_store_revision,
            "expected_store_status": self.expected_store_status,
            "expected_store_tradeoffer_id": self.expected_store_tradeoffer_id,
            "created_at": self.created_at,
        }

    def metadata(self, *, phase: str) -> dict[str, object]:
        if phase not in {"armed", "completed", "retired"}:
            raise CanaryAuthorityError("invalid_authority_phase")
        return {"version": _METADATA_VERSION, "phase": phase, **self.metadata_fields()}


@dataclass(frozen=True)
class CanaryWriteTarget:
    """Secret-free identity supplied at the last safe write boundary."""
    action: str
    purchase_id: str | None = None
    buff_order_id: str | None = None
    account_id: str | None = None
    recipient_steam_id: str | None = None
    host_db_id: int | None = None
    assetid: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _ALL_ACTIONS:
            raise CanaryAuthorityError("invalid_write_action")
        for field in ("purchase_id", "buff_order_id", "account_id", "recipient_steam_id", "assetid"):
            value = getattr(self, field)
            if value is not None:
                _exact_text(value, field=field)
        if self.host_db_id is not None:
            _exact_positive_int(self.host_db_id, field="host_db_id")


def _production_root() -> Path:
    """Resolve one OS-owned user namespace, not an app/env-selected path."""
    if os.name == "nt":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0028, None, 0, buffer)
            if result != 0 or not buffer.value:
                raise OSError("profile_lookup_failed")
            return Path(buffer.value) / _AUTHORITY_DIR_NAME
        except Exception as exc:
            raise CanaryAuthorityError("authority_user_scope_unavailable") from exc
    if os.name == "posix":
        try:
            import pwd
            home = pwd.getpwuid(os.getuid()).pw_dir
            if not home:
                raise OSError("profile_lookup_failed")
            return Path(home) / _AUTHORITY_DIR_NAME
        except Exception as exc:
            raise CanaryAuthorityError("authority_user_scope_unavailable") from exc
    raise CanaryAuthorityError("authority_platform_unsupported")


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _try_lock_file(handle) -> bool:
    if os.name == "nt":
        import msvcrt
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _permit_from_record(data: dict[str, object]) -> CanaryPermit:
    host_ids = data.get("expected_host_order_ids")
    if type(host_ids) is not list or len(host_ids) != 1:
        raise CanaryAuthorityError("authority_metadata_invalid")
    try:
        return CanaryPermit(
            permit_id=data.get("permit_id"),
            owner_nonce=data.get("owner_nonce"),
            host_db_id=data.get("host_db_id"),
            buff_order_id=data.get("buff_order_id"),
            purchase_id=data.get("purchase_id"),
            account_id=data.get("account_id"),
            recipient_steam_id=data.get("recipient_steam_id"),
            expected_host_order_ids=tuple(host_ids),
            expected_store_present=data.get("expected_store_present"),
            expected_store_revision=data.get("expected_store_revision"),
            expected_store_status=data.get("expected_store_status"),
            expected_store_tradeoffer_id=data.get("expected_store_tradeoffer_id"),
            created_at=data.get("created_at"),
        )
    except Exception as exc:
        raise CanaryAuthorityError("authority_metadata_invalid") from exc


def _used_permit_ids(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_USED_PERMIT_IDS:
        raise CanaryAuthorityError("authority_metadata_invalid")
    result: list[str] = []
    for raw in value:
        permit_id = _exact_text(raw, field="metadata_used_permit_id")
        if permit_id in result:
            raise CanaryAuthorityError("authority_metadata_invalid")
        result.append(permit_id)
    return tuple(result)


class _CanaryOwnerSession:
    """Opaque process-local capability retained only by the target Host integration."""
    __slots__ = ("_authority", "_capability")

    def __init__(self, authority: "CanaryAuthority", capability: object) -> None:
        self._authority = authority
        self._capability = capability

    def __repr__(self) -> str:
        return "<opaque canary owner session>"

    def __reduce__(self):
        raise TypeError("canary_owner_session_not_serializable")

    def __copy__(self):
        raise TypeError("canary_owner_session_not_serializable")

    def __deepcopy__(self, _memo):
        raise TypeError("canary_owner_session_not_serializable")

    def runtime_guard(self):
        return self._authority._owner_session_runtime_guard(self._capability)

    def external_write_guard(self, target: CanaryWriteTarget):
        return self._authority._owner_session_external_write_guard(self._capability, target)

    def mark_completed(self) -> None:
        self._authority._owner_session_mark_completed(self._capability)

    def release_keep_fence(self) -> None:
        self._authority._owner_session_release(self._capability)


class CanaryAuthority:
    """Own or fence one canary namespace without owning delivery state."""
    def __init__(self, *, _root: Path | None = None, _host_db_path: Path | None = None) -> None:
        self._root = _production_root() if _root is None else Path(_root)
        self._lock_path = self._root / _LOCK_FILE_NAME
        self._metadata_path = self._root / _METADATA_FILE_NAME
        self._host_db_path = (
            _PRODUCTION_HOST_DB_PATH
            if _root is None and _host_db_path is None
            else (Path(_host_db_path) if _host_db_path is not None else None)
        )
        self._owner_handle = None
        self._owner_permit: CanaryPermit | None = None
        self._owner_phase: str | None = None
        self._owner_generation: int | None = None
        self._owner_capability: object | None = None
        self._owner_active_write: CanaryWriteTarget | None = None
        self._owner_receipt_refinement_consumed = False
        self._normal_guard_depth = 0
        self._thread_lock = threading.RLock()

    @property
    def owns_canary(self) -> bool:
        with self._thread_lock:
            return self._owner_handle is not None and self._owner_permit is not None

    def _read_record(self) -> dict[str, object] | None:
        try:
            raw = self._metadata_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CanaryAuthorityError("authority_metadata_read_failed") from exc
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise CanaryAuthorityError("authority_metadata_invalid") from exc
        if not isinstance(data, dict) or data.get("version") != _METADATA_VERSION:
            raise CanaryAuthorityError("authority_metadata_invalid")
        if data.get("phase") not in {"armed", "completed", "retired"}:
            raise CanaryAuthorityError("authority_metadata_invalid")
        generation = _exact_positive_int(data.get("generation"), field="metadata_generation")
        used = _used_permit_ids(data.get("used_permit_ids"))
        permit = _permit_from_record(data)
        if permit.permit_id not in used:
            raise CanaryAuthorityError("authority_metadata_invalid")
        record = dict(data)
        record["generation"] = generation
        record["used_permit_ids"] = list(used)
        record["_permit"] = permit
        return record

    @staticmethod
    def _active_record(record: dict[str, object] | None) -> bool:
        return record is not None and record.get("phase") in _ACTIVE_PHASES

    def has_canary_metadata(self) -> bool:
        return self._read_record() is not None

    def has_active_fence(self) -> bool:
        return self._active_record(self._read_record())

    @staticmethod
    def _next_used_ids(existing: tuple[str, ...], permit_id: str) -> tuple[str, ...]:
        permit_id = _exact_text(permit_id, field="permit_id")
        if permit_id in existing:
            raise CanaryAuthorityStaleError("authority_permit_replay")
        if len(existing) >= _MAX_USED_PERMIT_IDS:
            raise CanaryAuthorityError("authority_used_permit_limit")
        return (*existing, permit_id)

    def _make_record(self, permit: CanaryPermit, *, phase: str, generation: int, used_permit_ids: tuple[str, ...]) -> dict[str, object]:
        if phase not in {"armed", "completed", "retired"}:
            raise CanaryAuthorityError("invalid_authority_phase")
        _exact_positive_int(generation, field="generation")
        if permit.permit_id not in used_permit_ids:
            raise CanaryAuthorityError("authority_used_permit_missing")
        return {
            "version": _METADATA_VERSION,
            "phase": phase,
            "generation": generation,
            "used_permit_ids": list(used_permit_ids),
            **permit.metadata_fields(),
        }

    def _write_record(self, record: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        fd, temporary_name = tempfile.mkstemp(prefix="live-canary-", suffix=".tmp", dir=str(self._root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._metadata_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise CanaryAuthorityError("authority_metadata_write_failed") from None

    def _open_lock_handle(self):
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            return open(self._lock_path, "a+b", buffering=0)
        except OSError as exc:
            raise CanaryAuthorityError("authority_lock_open_failed") from exc

    def _set_owner(self, handle, permit: CanaryPermit, *, phase: str, generation: int) -> _CanaryOwnerSession:
        capability = object()
        self._owner_handle = handle
        self._owner_permit = permit
        self._owner_phase = phase
        self._owner_generation = generation
        self._owner_capability = capability
        self._owner_active_write = None
        self._owner_receipt_refinement_consumed = False
        return _CanaryOwnerSession(self, capability)

    def _arm_owner_session(self, permit: CanaryPermit) -> _CanaryOwnerSession:
        if type(permit) is not CanaryPermit:
            raise CanaryAuthorityError("invalid_canary_permit")
        CanaryPermit.__post_init__(permit)
        with self._thread_lock:
            if self._owner_handle is not None:
                raise CanaryAuthorityBusyError("authority_already_owned")
            handle = self._open_lock_handle()
            if not _try_lock_file(handle):
                handle.close()
                raise CanaryAuthorityBusyError("authority_busy")
            try:
                record = self._read_record()
                if self._active_record(record):
                    raise CanaryAuthorityStaleError("authority_metadata_requires_recovery")
                generation = 1 if record is None else int(record["generation"]) + 1
                used = () if record is None else tuple(record["used_permit_ids"])
                used = self._next_used_ids(used, permit.permit_id)
                self._write_record(self._make_record(
                    permit, phase="armed", generation=generation, used_permit_ids=used
                ))
            except Exception:
                _unlock_file(handle)
                handle.close()
                raise
            return self._set_owner(handle, permit, phase="armed", generation=generation)

    def arm(self, permit: CanaryPermit) -> None:
        if type(permit) is not CanaryPermit:
            raise CanaryAuthorityError("invalid_canary_permit")
        raise CanaryAuthorityError("canary_host_activation_required")

    def _recover_owner_session(self, *, expected_old_permit_id: str, new_permit: CanaryPermit) -> _CanaryOwnerSession:
        old_id = _exact_text(expected_old_permit_id, field="expected_old_permit_id")
        if type(new_permit) is not CanaryPermit:
            raise CanaryAuthorityError("invalid_canary_permit")
        CanaryPermit.__post_init__(new_permit)
        with self._thread_lock:
            if self._owner_handle is not None:
                raise CanaryAuthorityBusyError("authority_live_owner_present")
            handle = self._open_lock_handle()
            if not _try_lock_file(handle):
                handle.close()
                raise CanaryAuthorityBusyError("authority_busy")
            try:
                record = self._read_record()
                if not self._active_record(record):
                    raise CanaryAuthorityStaleError("authority_active_metadata_required")
                if record.get("permit_id") != old_id:
                    raise CanaryAuthorityStaleError("authority_permit_mismatch")
                used = self._next_used_ids(tuple(record["used_permit_ids"]), new_permit.permit_id)
                generation = int(record["generation"]) + 1
                self._write_record(self._make_record(
                    new_permit, phase="armed", generation=generation, used_permit_ids=used
                ))
            except Exception:
                _unlock_file(handle)
                handle.close()
                raise
            return self._set_owner(handle, new_permit, phase="armed", generation=generation)

    def recover_and_rearm(self, *, expected_old_permit_id: str, new_permit: CanaryPermit) -> None:
        _exact_text(expected_old_permit_id, field="expected_old_permit_id")
        if type(new_permit) is not CanaryPermit:
            raise CanaryAuthorityError("invalid_canary_permit")
        raise CanaryAuthorityError("canary_host_recovery_required")

    def clear_stale(self, *, expected_permit_id: str) -> None:
        _exact_text(expected_permit_id, field="expected_permit_id")
        raise CanaryAuthorityError("clear_stale_disabled_use_atomic_recovery")

    def retire_stale(self, *, expected_permit_id: str) -> None:
        permit_id = _exact_text(expected_permit_id, field="expected_permit_id")
        with self._thread_lock:
            if self._owner_handle is not None:
                raise CanaryAuthorityBusyError("authority_live_owner_present")
            handle = self._open_lock_handle()
            if not _try_lock_file(handle):
                handle.close()
                raise CanaryAuthorityBusyError("authority_busy")
            try:
                record = self._read_record()
                if record is None or record.get("phase") != "completed":
                    raise CanaryAuthorityStaleError("authority_completed_metadata_required")
                if record.get("permit_id") != permit_id:
                    raise CanaryAuthorityStaleError("authority_permit_mismatch")
                self._write_record(self._make_record(
                    record["_permit"],
                    phase="retired",
                    generation=int(record["generation"]),
                    used_permit_ids=tuple(record["used_permit_ids"]),
                ))
            finally:
                _unlock_file(handle)
                handle.close()

    def validates_owner_session(self, session: object, permit: CanaryPermit) -> bool:
        with self._thread_lock:
            return (
                type(session) is _CanaryOwnerSession
                and session._authority is self
                and session._capability is self._owner_capability
                and self._owner_handle is not None
                and self._owner_permit == permit
                and self._owner_generation is not None
                and self._owner_phase in _ACTIVE_PHASES
            )

    def _require_owner_capability(self, capability: object, *, allow_completed: bool = True) -> CanaryPermit:
        permit = self._owner_permit
        if (
            capability is not self._owner_capability
            or permit is None
            or self._owner_handle is None
            or self._owner_generation is None
        ):
            raise CanaryWriteBlockedError("canary_owner_session_required")
        allowed_phases = _ACTIVE_PHASES if allow_completed else frozenset({"armed"})
        if self._owner_phase not in allowed_phases:
            raise CanaryWriteBlockedError("canary_not_armed")
        return permit

    def _pending_host_rows(self, connection) -> list[tuple[object, ...]]:
        return list(connection.execute(
            "SELECT id, buff_order_id, pending_receipt, assetid "
            "FROM purchase WHERE pending_receipt = 1"
        ).fetchall())

    def _require_exact_live_host_target(self, rows: list[tuple[object, ...]]) -> None:
        permit = self._owner_permit
        if permit is None or len(rows) != 1:
            raise CanaryWriteBlockedError("canary_host_target_not_exclusive")
        db_id, order_id, pending_receipt, assetid = rows[0]
        if (
            db_id != permit.host_db_id
            or order_id != permit.buff_order_id
            or pending_receipt not in (1, True)
            or assetid not in (None, "")
        ):
            raise CanaryWriteBlockedError("canary_host_target_mismatch")

    @contextmanager
    def _host_db_write_barrier(self, target: CanaryWriteTarget) -> Iterator[None]:
        if target.action not in {"auto_offer_send", "auto_offer_confirm"}:
            yield
            return
        if self._host_db_path is None:
            raise CanaryWriteBlockedError("canary_host_db_barrier_required")
        connection = None
        try:
            connection = sqlite3.connect(str(self._host_db_path), timeout=0.0, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            self._require_exact_live_host_target(self._pending_host_rows(connection))
            yield
        except CanaryWriteBlockedError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError):
            raise CanaryWriteBlockedError("canary_host_db_barrier_failed") from None
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                connection.close()

    def _require_no_pending_host_rows_for_completion(self) -> None:
        if self._host_db_path is None:
            return
        connection = None
        try:
            connection = sqlite3.connect(str(self._host_db_path), timeout=0.0, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            if self._pending_host_rows(connection):
                raise CanaryAuthorityError("canary_completion_host_pending")
        except CanaryAuthorityError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError):
            raise CanaryAuthorityError("canary_completion_host_check_failed") from None
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                connection.close()

    def _authorize_owner_write(self, permit: CanaryPermit, target: CanaryWriteTarget) -> None:
        if self._owner_phase != "armed":
            raise CanaryWriteBlockedError("canary_not_armed")
        if target.action in _DENIED_DURING_CANARY_ACTIONS:
            raise CanaryWriteBlockedError("write_denied_during_canary")
        if target.action not in _ALLOWED_OWNER_ACTIONS:
            raise CanaryWriteBlockedError("write_not_allowlisted")
        if (
            target.purchase_id != permit.purchase_id
            or target.buff_order_id != permit.buff_order_id
            or target.account_id != permit.account_id
            or target.recipient_steam_id != permit.recipient_steam_id
        ):
            raise CanaryWriteBlockedError("canary_target_mismatch")
        if target.action == "host_receipt":
            if target.host_db_id != permit.host_db_id or target.assetid is None:
                raise CanaryWriteBlockedError("canary_host_receipt_identity_required")
        elif target.host_db_id is not None or target.assetid is not None:
            raise CanaryWriteBlockedError("canary_write_target_excess_identity")

    @contextmanager
    def _owner_session_runtime_guard(self, capability: object) -> Iterator[None]:
        with self._thread_lock:
            self._require_owner_capability(capability, allow_completed=True)
            yield

    def owner_runtime_guard(self, _permit: CanaryPermit):
        raise CanaryAuthorityError("canary_owner_session_required")

    @contextmanager
    def runtime_guard(self) -> Iterator[None]:
        with self._thread_lock:
            if self._owner_handle is not None:
                raise CanaryAuthorityBusyError("canary_authority_active")
            if self._normal_guard_depth:
                self._normal_guard_depth += 1
                try:
                    yield
                finally:
                    self._normal_guard_depth -= 1
                return
            handle = self._open_lock_handle()
            if not _try_lock_file(handle):
                handle.close()
                raise CanaryAuthorityBusyError("canary_authority_active")
            try:
                if self._active_record(self._read_record()):
                    raise CanaryAuthorityStaleError("canary_authority_fenced")
                self._normal_guard_depth = 1
                try:
                    yield
                finally:
                    self._normal_guard_depth = 0
            finally:
                _unlock_file(handle)
                handle.close()

    def _nested_receipt_refinement_allowed(self, target: CanaryWriteTarget) -> bool:
        outer = self._owner_active_write
        permit = self._owner_permit
        if outer is None or permit is None or outer.action != "host_receipt" or target.action != "host_receipt":
            return False
        return (
            target.purchase_id is None
            and target.account_id is None
            and target.recipient_steam_id is None
            and target.buff_order_id == permit.buff_order_id
            and target.host_db_id == permit.host_db_id
            and target.assetid == outer.assetid
        )

    @contextmanager
    def _owner_session_external_write_guard(self, capability: object, target: CanaryWriteTarget) -> Iterator[None]:
        if type(target) is not CanaryWriteTarget:
            raise CanaryAuthorityError("invalid_write_target")
        CanaryWriteTarget.__post_init__(target)
        with self._thread_lock:
            permit = self._require_owner_capability(capability, allow_completed=False)
            if self._owner_active_write is not None:
                raise CanaryWriteBlockedError("owner_write_reentry_forbidden")
            self._authorize_owner_write(permit, target)
            self._owner_active_write = target
            self._owner_receipt_refinement_consumed = False
            try:
                with self._host_db_write_barrier(target):
                    yield
            finally:
                self._owner_active_write = None
                self._owner_receipt_refinement_consumed = False

    @contextmanager
    def external_write_guard(self, target: CanaryWriteTarget) -> Iterator[None]:
        if type(target) is not CanaryWriteTarget:
            raise CanaryAuthorityError("invalid_write_target")
        CanaryWriteTarget.__post_init__(target)
        with self._thread_lock:
            if self._owner_handle is not None:
                if self._nested_receipt_refinement_allowed(target):
                    if self._owner_receipt_refinement_consumed:
                        raise CanaryWriteBlockedError("nested_receipt_already_consumed")
                    self._owner_receipt_refinement_consumed = True
                    yield
                    return
                raise CanaryWriteBlockedError("canary_owner_session_required")
            if self._normal_guard_depth:
                self._normal_guard_depth += 1
                try:
                    yield
                finally:
                    self._normal_guard_depth -= 1
                return
            handle = self._open_lock_handle()
            if not _try_lock_file(handle):
                handle.close()
                raise CanaryWriteBlockedError("canary_authority_active")
            try:
                if self._active_record(self._read_record()):
                    raise CanaryWriteBlockedError("canary_authority_fenced")
                self._normal_guard_depth = 1
                try:
                    yield
                finally:
                    self._normal_guard_depth = 0
            finally:
                _unlock_file(handle)
                handle.close()

    def _owner_session_mark_completed(self, capability: object) -> None:
        with self._thread_lock:
            self._require_owner_capability(capability, allow_completed=False)
            self._require_no_pending_host_rows_for_completion()
            record = self._read_record()
            if (
                record is None
                or record.get("phase") != "armed"
                or record.get("permit_id") != self._owner_permit.permit_id
                or record.get("generation") != self._owner_generation
            ):
                raise CanaryAuthorityError("authority_generation_mismatch")
            self._write_record(self._make_record(
                self._owner_permit,
                phase="completed",
                generation=self._owner_generation,
                used_permit_ids=tuple(record["used_permit_ids"]),
            ))
            self._owner_phase = "completed"

    def mark_completed(self) -> None:
        raise CanaryAuthorityError("canary_owner_session_required")

    def _owner_session_release(self, capability: object) -> None:
        with self._thread_lock:
            self._require_owner_capability(capability, allow_completed=True)
            handle = self._owner_handle
            self._owner_handle = None
            self._owner_permit = None
            self._owner_phase = None
            self._owner_generation = None
            self._owner_capability = None
            self._owner_active_write = None
            self._owner_receipt_refinement_consumed = False
            self._normal_guard_depth = 0
            if handle is not None:
                _unlock_file(handle)
                handle.close()

    def release_keep_fence(self) -> None:
        raise CanaryAuthorityError("canary_owner_session_required")


_PRODUCTION_AUTHORITY = CanaryAuthority()


def get_canary_authority() -> CanaryAuthority:
    return _PRODUCTION_AUTHORITY


def canary_metadata_present() -> bool:
    """Retired history is inert; armed/completed generations fence writers."""
    return get_canary_authority().has_active_fence()


@contextmanager
def external_write_guard(
    action: str,
    *,
    purchase_id: str | None = None,
    buff_order_id: str | None = None,
    account_id: str | None = None,
    recipient_steam_id: str | None = None,
    host_db_id: int | None = None,
    assetid: str | None = None,
) -> Iterator[None]:
    target = CanaryWriteTarget(
        action=action,
        purchase_id=purchase_id,
        buff_order_id=buff_order_id,
        account_id=account_id,
        recipient_steam_id=recipient_steam_id,
        host_db_id=host_db_id,
        assetid=assetid,
    )
    with get_canary_authority().external_write_guard(target):
        yield


__all__ = [
    "CanaryAuthority",
    "CanaryAuthorityBusyError",
    "CanaryAuthorityError",
    "CanaryAuthorityStaleError",
    "CanaryPermit",
    "CanaryWriteBlockedError",
    "CanaryWriteTarget",
    "canary_metadata_present",
    "external_write_guard",
    "get_canary_authority",
]
