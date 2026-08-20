"""Detached, structurally zero-write local snapshot collection for live-canary preflight."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import (
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
)
from .store import AUTO_OFFER_STORE_SCHEMA_VERSION

_STORE_TABLE: Final[str] = "auto_offer_delivery"
_STORE_SELECT: Final[str] = (
    "id, purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, counterparty_steam_id, revision"
)
_STORE_EXPECTED_COLUMNS: Final[tuple[tuple[str, str, int, int], ...]] = (
    ("id", "INTEGER", 0, 1),
    ("purchase_id", "TEXT", 1, 0),
    ("buff_order_id", "TEXT", 1, 0),
    ("account_id", "TEXT", 1, 0),
    ("recipient_steam_id", "TEXT", 1, 0),
    ("delivery_mode", "TEXT", 0, 0),
    ("delivery_status", "TEXT", 1, 0),
    ("steam_tradeoffer_id", "TEXT", 0, 0),
    ("offer_attempted_at", "REAL", 0, 0),
    ("offer_sent_at", "REAL", 0, 0),
    ("received_at", "REAL", 0, 0),
    ("delivery_error", "TEXT", 0, 0),
    ("pending_receipt", "INTEGER", 1, 0),
    ("assetid", "TEXT", 0, 0),
    ("revision", "INTEGER", 1, 0),
    ("counterparty_steam_id", "TEXT", 0, 0),
)
_SOURCE_SUFFIXES: Final[tuple[str, ...]] = ("", "-wal", "-shm", "-journal")


class PreflightSnapshotError(RuntimeError):
    """Fail-closed local snapshot collection error with fixed reason codes."""


@dataclass(frozen=True, slots=True)
class HostPendingPurchaseSnapshot:
    """Minimum secret-free Host Purchase identity required by the canary gate."""

    host_db_id: int
    buff_order_id: str
    assetid: str | None


@dataclass(frozen=True, slots=True)
class AutoOfferDeliverySnapshot:
    """Detached minimum Auto Offer delivery state required by the canary gate."""

    purchase_id: str
    buff_order_id: str
    account_id: str
    recipient_steam_id: str
    delivery_mode: DeliveryMode | None
    delivery_status: DeliveryStatus
    steam_tradeoffer_id: str | None
    pending_receipt: bool
    assetid: str | None
    counterparty_steam_id: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class LocalPreflightSnapshot:
    """Immutable local evidence; it contains no mutation or platform capability."""

    host_pending: tuple[HostPendingPurchaseSnapshot, ...]
    store_exists: bool
    store_rows: tuple[AutoOfferDeliverySnapshot, ...]


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    exists: bool
    device: int | None
    inode: int | None
    size: int | None
    mtime_ns: int | None
    sha256: str | None


def _path(value: object, *, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise PreflightSnapshotError(f"invalid_{field}")
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise PreflightSnapshotError(f"invalid_{field}") from None
    if not path.name:
        raise PreflightSnapshotError(f"invalid_{field}")
    return path


def _source_family(path: Path) -> tuple[Path, ...]:
    text = str(path)
    return tuple(path if not suffix else Path(text + suffix) for suffix in _SOURCE_SUFFIXES)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise PreflightSnapshotError("source_changed_during_collection") from None
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple[_FileFingerprint, ...]:
    fingerprints: list[_FileFingerprint] = []
    for candidate in _source_family(path):
        try:
            exists = candidate.exists()
        except OSError:
            raise PreflightSnapshotError("source_changed_during_collection") from None
        if not exists:
            fingerprints.append(_FileFingerprint(False, None, None, None, None, None))
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise PreflightSnapshotError("invalid_source_file")
            stat = candidate.stat()
        except OSError:
            raise PreflightSnapshotError("source_changed_during_collection") from None
        fingerprints.append(
            _FileFingerprint(
                True,
                getattr(stat, "st_dev", None),
                getattr(stat, "st_ino", None),
                stat.st_size,
                stat.st_mtime_ns,
                _hash_file(candidate),
            )
        )
    return tuple(fingerprints)


def _require_quiescent(
    path: Path,
    fingerprint: tuple[_FileFingerprint, ...],
    *,
    allow_absent: bool,
) -> bool:
    main = fingerprint[0]
    if not main.exists:
        if any(item.exists for item in fingerprint[1:]):
            raise PreflightSnapshotError("orphan_sqlite_sidecar")
        if allow_absent:
            return False
        raise PreflightSnapshotError("host_db_missing")
    if any(item.exists for item in fingerprint[1:]):
        raise PreflightSnapshotError("sqlite_source_not_quiescent")
    return True


def _read_main_bytes(path: Path) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError:
        raise PreflightSnapshotError("source_changed_during_collection") from None
    if len(payload) < 100 or payload[:16] != b"SQLite format 3\x00":
        raise PreflightSnapshotError("invalid_sqlite_source")
    if payload[18:20] == b"\x01\x01":
        return payload
    if payload[18:20] == b"\x02\x02":
        # A cleanly closed WAL database keeps WAL read/write-version bytes in
        # the main-file header even after its WAL/SHM sidecars are removed.
        # Normalize only the detached copy so SQLite can deserialize it into
        # memory without ever opening or mutating the source file.
        detached = bytearray(payload)
        detached[18] = 1
        detached[19] = 1
        return bytes(detached)
    raise PreflightSnapshotError("invalid_sqlite_source")


def _deserialize(payload: bytes) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        deserialize = getattr(connection, "deserialize", None)
        if not callable(deserialize):
            raise PreflightSnapshotError("sqlite_deserialize_unavailable")
        deserialize(payload)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("PRAGMA query_only").fetchone()
        if row is None or row[0] != 1:
            raise PreflightSnapshotError("sqlite_query_only_unavailable")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection
    except PreflightSnapshotError:
        connection.close()
        raise
    except sqlite3.DatabaseError:
        connection.close()
        raise PreflightSnapshotError("invalid_sqlite_source") from None


def _strict_text(value: object, *, reason: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 32 for character in value)
    ):
        raise PreflightSnapshotError(reason)
    return value


def _optional_text(value: object, *, reason: str) -> str | None:
    return None if value is None else _strict_text(value, reason=reason)


def _collect_host(payload: bytes) -> tuple[HostPendingPurchaseSnapshot, ...]:
    connection = _deserialize(payload)
    try:
        table_info = connection.execute("PRAGMA table_info(purchase)").fetchall()
        if not table_info:
            raise PreflightSnapshotError("host_purchase_schema_mismatch")
        columns = {str(row[1]): row for row in table_info}
        required = {"id", "buff_order_id", "pending_receipt", "assetid"}
        if required - set(columns):
            raise PreflightSnapshotError("host_purchase_schema_mismatch")
        if columns["id"][5] != 1 or "INT" not in str(columns["id"][2]).upper():
            raise PreflightSnapshotError("host_purchase_schema_mismatch")

        rows = connection.execute(
            "SELECT id, buff_order_id, pending_receipt, assetid "
            "FROM purchase WHERE pending_receipt = 1 ORDER BY id ASC"
        ).fetchall()
    except PreflightSnapshotError:
        raise
    except sqlite3.DatabaseError:
        raise PreflightSnapshotError("host_purchase_read_failed") from None
    finally:
        connection.close()

    snapshots: list[HostPendingPurchaseSnapshot] = []
    seen_orders: set[str] = set()
    for row in rows:
        if len(row) != 4 or type(row[0]) is not int or row[0] <= 0:
            raise PreflightSnapshotError("host_purchase_row_invalid")
        order_id = _strict_text(row[1], reason="host_purchase_row_invalid")
        if type(row[2]) is not int or row[2] != 1:
            raise PreflightSnapshotError("host_purchase_row_invalid")
        assetid = _optional_text(row[3], reason="host_purchase_row_invalid")
        if order_id in seen_orders:
            raise PreflightSnapshotError("duplicate_host_buff_order_id")
        seen_orders.add(order_id)
        snapshots.append(
            HostPendingPurchaseSnapshot(
                host_db_id=row[0],
                buff_order_id=order_id,
                assetid=assetid,
            )
        )
    return tuple(snapshots)


def _store_user_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _validate_store_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if (
        version_row is None
        or type(version_row[0]) is not int
        or version_row[0] != AUTO_OFFER_STORE_SCHEMA_VERSION
    ):
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")
    if _store_user_tables(connection) != {_STORE_TABLE}:
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")

    table_info = connection.execute(f"PRAGMA table_info({_STORE_TABLE})").fetchall()
    actual_columns = tuple(
        (str(row[1]), str(row[2]).upper(), row[3], row[5]) for row in table_info
    )
    if actual_columns != _STORE_EXPECTED_COLUMNS:
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")

    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_STORE_TABLE,),
    ).fetchone()
    if not table_sql or "AUTOINCREMENT" not in str(table_sql[0]).upper():
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")

    unique_columns: set[tuple[str, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list({_STORE_TABLE})").fetchall():
        if index[2] != 1:
            continue
        index_name = str(index[1]).replace('"', '""')
        info = connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        unique_columns.add(tuple(str(item[2]) for item in info))
    if {("purchase_id",), ("buff_order_id",)} - unique_columns:
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")


def _store_row(row: tuple[object, ...]) -> AutoOfferDeliverySnapshot:
    if len(row) != 16 or type(row[0]) is not int or row[0] <= 0:
        raise PreflightSnapshotError("auto_offer_store_row_invalid")
    if type(row[12]) is not int or row[12] not in (0, 1):
        raise PreflightSnapshotError("auto_offer_store_row_invalid")
    if type(row[15]) is not int or row[15] <= 0:
        raise PreflightSnapshotError("auto_offer_store_row_invalid")
    try:
        mode = None if row[5] is None else DeliveryMode(row[5])
        status = DeliveryStatus(row[6])
        snapshot = DeliverySnapshot(
            purchase_id=row[1],
            buff_order_id=row[2],
            account_id=row[3],
            recipient_steam_id=row[4],
            delivery_mode=mode,
            delivery_status=status,
            steam_tradeoffer_id=row[7],
            offer_attempted_at=row[8],
            offer_sent_at=row[9],
            received_at=row[10],
            delivery_error=row[11],
            pending_receipt=bool(row[12]),
            assetid=row[13],
            counterparty_steam_id=row[14],
        )
        validate_delivery_snapshot(snapshot)
    except (DeliveryContractError, TypeError, ValueError):
        raise PreflightSnapshotError("auto_offer_store_row_invalid") from None

    return AutoOfferDeliverySnapshot(
        purchase_id=snapshot.purchase_id,
        buff_order_id=snapshot.buff_order_id,
        account_id=snapshot.account_id,
        recipient_steam_id=snapshot.recipient_steam_id,
        delivery_mode=snapshot.delivery_mode,
        delivery_status=snapshot.delivery_status,
        steam_tradeoffer_id=snapshot.steam_tradeoffer_id,
        pending_receipt=snapshot.pending_receipt,
        assetid=snapshot.assetid,
        counterparty_steam_id=snapshot.counterparty_steam_id,
        revision=row[15],
    )


def _collect_store(payload: bytes) -> tuple[AutoOfferDeliverySnapshot, ...]:
    connection = _deserialize(payload)
    try:
        _validate_store_schema(connection)
        rows = connection.execute(
            f"SELECT {_STORE_SELECT} FROM {_STORE_TABLE} ORDER BY id ASC"
        ).fetchall()
    except PreflightSnapshotError:
        raise
    except sqlite3.DatabaseError:
        raise PreflightSnapshotError("auto_offer_store_read_failed") from None
    finally:
        connection.close()

    snapshots = tuple(_store_row(row) for row in rows)
    purchases = [item.purchase_id for item in snapshots]
    orders = [item.buff_order_id for item in snapshots]
    if len(set(purchases)) != len(purchases) or len(set(orders)) != len(orders):
        raise PreflightSnapshotError("duplicate_auto_offer_identity")
    return snapshots


def collect_local_preflight_snapshot(
    *,
    host_db_path: object,
    auto_offer_store_path: object,
) -> LocalPreflightSnapshot:
    """Collect one detached local snapshot without opening either source via SQLite."""

    host_path = _path(host_db_path, field="host_db_path")
    store_path = _path(auto_offer_store_path, field="auto_offer_store_path")

    host_before = _fingerprint(host_path)
    store_before = _fingerprint(store_path)
    _require_quiescent(host_path, host_before, allow_absent=False)
    store_exists = _require_quiescent(store_path, store_before, allow_absent=True)
    if store_exists and store_before[0].size == 0:
        raise PreflightSnapshotError("auto_offer_store_schema_mismatch")

    host_payload = _read_main_bytes(host_path)
    store_payload = _read_main_bytes(store_path) if store_exists else None

    host_pending = _collect_host(host_payload)
    store_rows = () if store_payload is None else _collect_store(store_payload)

    host_after = _fingerprint(host_path)
    store_after = _fingerprint(store_path)
    if host_before != host_after or store_before != store_after:
        raise PreflightSnapshotError("source_changed_during_collection")

    return LocalPreflightSnapshot(
        host_pending=host_pending,
        store_exists=store_exists,
        store_rows=store_rows,
    )


__all__ = [
    "AutoOfferDeliverySnapshot",
    "HostPendingPurchaseSnapshot",
    "LocalPreflightSnapshot",
    "PreflightSnapshotError",
    "collect_local_preflight_snapshot",
]
