"""Independent, fail-closed SQLite persistence for native Auto Offer state."""

from __future__ import annotations

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
    validate_delivery_transition,
)


AUTO_OFFER_STORE_SCHEMA_VERSION: Final[int] = 1
_TABLE_NAME: Final[str] = "auto_offer_delivery"
_RECOVERABLE_STATUSES: Final[frozenset[DeliveryStatus]] = frozenset(
    {
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.RESULT_UNKNOWN,
    }
)
_SELECT_COLUMNS: Final[str] = (
    "id, purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, revision"
)
_INSERT_COLUMNS: Final[str] = (
    "purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, revision"
)
_EXPECTED_COLUMNS: Final[tuple[tuple[str, str, int, int], ...]] = (
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
)
_CREATE_TABLE_SQL: Final[str] = f"""
CREATE TABLE {_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id TEXT NOT NULL UNIQUE,
    buff_order_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    recipient_steam_id TEXT NOT NULL,
    delivery_mode TEXT NULL,
    delivery_status TEXT NOT NULL,
    steam_tradeoffer_id TEXT NULL,
    offer_attempted_at REAL NULL,
    offer_sent_at REAL NULL,
    received_at REAL NULL,
    delivery_error TEXT NULL,
    pending_receipt INTEGER NOT NULL,
    assetid TEXT NULL,
    revision INTEGER NOT NULL
)
"""


class AutoOfferStoreError(RuntimeError):
    """Base class for storage and safety failures."""


class AutoOfferStoreSchemaError(AutoOfferStoreError):
    """The database does not contain the exact supported schema."""


class AutoOfferStoreCorruptError(AutoOfferStoreError):
    """The database or a persisted row violates the storage contract."""


class AutoOfferStoreConflictError(AutoOfferStoreError):
    """The requested identity or state conflicts with committed state."""


class AutoOfferStoreStaleWriteError(AutoOfferStoreConflictError):
    """An optimistic-concurrency write used an old revision."""


@dataclass(frozen=True)
class StoredDelivery:
    snapshot: DeliverySnapshot
    revision: int


class AutoOfferStore:
    """A small explicit API around the independent Auto Offer database.

    Construction is intentionally side-effect free.  The caller must invoke
    :meth:`initialize` before any database operation.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open the database, configure SQLite, and create or verify schema v1."""
        if self._connection is not None:
            self._configure_connection(self._connection)
            self._validate_schema(self._connection)
            return

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self._db_path), timeout=5.0, isolation_level=None
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            raise AutoOfferStoreError("cannot open Auto Offer database") from exc

        self._connection = connection
        try:
            self._configure_connection(connection)
            version = self._user_version(connection)
            tables = self._user_tables(connection)
            if version == 0 and not tables:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(_CREATE_TABLE_SQL)
                    connection.execute(
                        f"PRAGMA user_version = {AUTO_OFFER_STORE_SCHEMA_VERSION}"
                    )
                    connection.commit()
                except (sqlite3.DatabaseError, AutoOfferStoreError):
                    self._rollback(connection)
                    raise
            else:
                self._validate_schema(connection)
        except AutoOfferStoreError:
            self.close()
            raise
        except sqlite3.DatabaseError as exc:
            self.close()
            raise AutoOfferStoreCorruptError(
                "SQLite rejected Auto Offer schema initialization"
            ) from exc

        self._validate_schema(connection)

    def close(self) -> None:
        """Close this store's connection without modifying the database."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.DatabaseError as exc:
                raise AutoOfferStoreError("cannot close Auto Offer database") from exc

    def ensure_initial(self, snapshot: DeliverySnapshot) -> StoredDelivery:
        """Insert one pending-direction delivery, or return its exact duplicate."""
        connection = self._ready_connection()
        self._validate_initial(snapshot)
        self._begin(connection)
        try:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE purchase_id = ? OR buff_order_id = ?",
                (snapshot.purchase_id, snapshot.buff_order_id),
            ).fetchall()
            if len(rows) > 1:
                raise AutoOfferStoreCorruptError(
                    "delivery identity maps to multiple persisted rows"
                )
            if rows:
                existing = self._row_to_stored(rows[0])
                if existing.snapshot != snapshot:
                    raise AutoOfferStoreConflictError(
                        "delivery identity conflicts with persisted snapshot"
                    )
                connection.commit()
                return existing

            connection.execute(
                f"INSERT INTO {_TABLE_NAME} ({_INSERT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._snapshot_values(snapshot) + (1,),
            )
            connection.commit()
            return StoredDelivery(snapshot=snapshot, revision=1)
        except AutoOfferStoreError:
            self._rollback(connection)
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback(connection)
            raise AutoOfferStoreConflictError(
                "delivery identity conflicts with persisted state"
            ) from exc
        except sqlite3.DatabaseError as exc:
            self._rollback(connection)
            raise AutoOfferStoreCorruptError(
                "SQLite rejected initial delivery write"
            ) from exc

    def get_by_purchase_id(self, purchase_id: str) -> StoredDelivery | None:
        return self._get_by("purchase_id", purchase_id)

    def get_by_buff_order_id(self, buff_order_id: str) -> StoredDelivery | None:
        return self._get_by("buff_order_id", buff_order_id)

    def advance(
        self, current: StoredDelivery, target: DeliverySnapshot
    ) -> StoredDelivery:
        """Persist exactly one contract-approved transition with CAS revision."""
        if type(current) is not StoredDelivery:
            raise AutoOfferStoreError("current must be a StoredDelivery")
        if type(current.revision) is not int or current.revision < 1:
            raise AutoOfferStoreCorruptError("current revision is invalid")
        try:
            validate_delivery_snapshot(current.snapshot)
            validate_delivery_snapshot(target)
        except DeliveryContractError:
            raise

        if (
            current.snapshot.steam_tradeoffer_id is not None
            and target.steam_tradeoffer_id != current.snapshot.steam_tradeoffer_id
        ):
            raise DeliveryContractError("bound steam trade offer ID cannot change")

        try:
            validate_delivery_transition(current.snapshot, target)
        except DeliveryContractError:
            raise

        if not self._same_identity(current.snapshot, target):
            raise DeliveryContractError("delivery identity cannot change")

        connection = self._ready_connection()
        self._begin(connection)
        try:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} WHERE purchase_id = ?",
                (current.snapshot.purchase_id,),
            ).fetchall()
            if len(rows) != 1:
                raise AutoOfferStoreStaleWriteError(
                    "current delivery no longer has one committed row"
                )
            persisted = self._row_to_stored(rows[0])
            if persisted.revision != current.revision:
                raise AutoOfferStoreStaleWriteError("delivery revision is stale")
            if persisted.snapshot != current.snapshot:
                raise AutoOfferStoreStaleWriteError(
                    "current delivery snapshot is stale"
                )

            cursor = connection.execute(
                f"UPDATE {_TABLE_NAME} SET "
                "account_id = ?, recipient_steam_id = ?, delivery_mode = ?, "
                "delivery_status = ?, steam_tradeoffer_id = ?, "
                "offer_attempted_at = ?, offer_sent_at = ?, received_at = ?, "
                "delivery_error = ?, pending_receipt = ?, assetid = ?, "
                "revision = revision + 1 "
                "WHERE purchase_id = ? AND revision = ?",
                self._snapshot_values(target)[2:]
                + (current.snapshot.purchase_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise AutoOfferStoreStaleWriteError("delivery revision is stale")
            connection.commit()
            return StoredDelivery(snapshot=target, revision=current.revision + 1)
        except AutoOfferStoreError:
            self._rollback(connection)
            raise
        except sqlite3.DatabaseError as exc:
            self._rollback(connection)
            raise AutoOfferStoreCorruptError(
                "SQLite rejected delivery transition"
            ) from exc

    def list_recoverable(self) -> list[StoredDelivery]:
        """Return all non-terminal candidates in deterministic insertion order."""
        connection = self._ready_connection()
        try:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} ORDER BY id ASC"
            ).fetchall()
            stored = [self._row_to_stored(row) for row in rows]
        except AutoOfferStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError(
                "SQLite rejected recovery candidate query"
            ) from exc
        return [item for item in stored if item.snapshot.delivery_status in _RECOVERABLE_STATUSES]

    def _get_by(self, column: str, value: str) -> StoredDelivery | None:
        if column not in {"purchase_id", "buff_order_id"}:
            raise AutoOfferStoreError("unsupported delivery lookup")
        if type(value) is not str or not value or value.strip() != value:
            raise AutoOfferStoreError(f"{column} must be a non-whitespace ID")
        connection = self._ready_connection()
        try:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} WHERE {column} = ?",
                (value,),
            ).fetchall()
            if len(rows) > 1:
                raise AutoOfferStoreCorruptError(
                    f"{column} maps to multiple persisted rows"
                )
            return None if not rows else self._row_to_stored(rows[0])
        except AutoOfferStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError(
                f"SQLite rejected {column} lookup"
            ) from exc

    def _ready_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise AutoOfferStoreError("AutoOfferStore.initialize() is required")
        self._configure_connection(connection)
        self._validate_schema(connection)
        return connection

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise AutoOfferStoreCorruptError("SQLite WAL mode was not enabled")
            connection.execute("PRAGMA synchronous = FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            if synchronous != 2:
                raise AutoOfferStoreCorruptError("SQLite FULL synchronous mode was not enabled")
            connection.execute("PRAGMA busy_timeout = 5000")
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            if busy_timeout != 5000:
                raise AutoOfferStoreCorruptError("SQLite busy timeout was not enabled")
        except AutoOfferStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot configure SQLite safely") from exc

    @staticmethod
    def _user_version(connection: sqlite3.Connection) -> int:
        try:
            value = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot read SQLite schema version") from exc
        if type(value) is not int or value < 0:
            raise AutoOfferStoreCorruptError("SQLite schema version is invalid")
        return value

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> set[str]:
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot inspect SQLite tables") from exc
        return {row[0] for row in rows}

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> None:
        version = cls._user_version(connection)
        tables = cls._user_tables(connection)
        if version > AUTO_OFFER_STORE_SCHEMA_VERSION:
            raise AutoOfferStoreSchemaError(f"unsupported schema version: {version}")
        if version == 0:
            if tables:
                raise AutoOfferStoreSchemaError(
                    "schema version 0 cannot contain Auto Offer tables"
                )
            return
        if version != AUTO_OFFER_STORE_SCHEMA_VERSION or tables != {_TABLE_NAME}:
            raise AutoOfferStoreSchemaError("Auto Offer schema does not match v1")

        try:
            table_info = connection.execute(
                f"PRAGMA table_info({_TABLE_NAME})"
            ).fetchall()
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_TABLE_NAME,),
            ).fetchone()
            indexes = connection.execute(
                f"PRAGMA index_list({_TABLE_NAME})"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot inspect Auto Offer schema") from exc

        actual_columns = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5]) for row in table_info
        )
        if actual_columns != _EXPECTED_COLUMNS:
            raise AutoOfferStoreSchemaError("Auto Offer table columns do not match v1")
        if not table_sql or "AUTOINCREMENT" not in str(table_sql[0]).upper():
            raise AutoOfferStoreSchemaError("Auto Offer id must use AUTOINCREMENT")

        unique_columns: set[tuple[str, ...]] = set()
        try:
            for index in indexes:
                if index[2] != 1:
                    continue
                index_name = str(index[1]).replace('"', '""')
                index_info = connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
                unique_columns.add(tuple(str(item[2]) for item in index_info))
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot inspect Auto Offer indexes") from exc
        if {("purchase_id",), ("buff_order_id",)} - unique_columns:
            raise AutoOfferStoreSchemaError(
                "purchase_id and buff_order_id must both be unique"
            )

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot begin Auto Offer transaction") from exc

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _validate_initial(snapshot: DeliverySnapshot) -> None:
        validate_delivery_snapshot(snapshot)
        if (
            snapshot.delivery_status is not DeliveryStatus.PENDING_DIRECTION
            or snapshot.delivery_mode is not None
            or snapshot.pending_receipt is not True
        ):
            raise DeliveryContractError(
                "ensure_initial requires pending_direction without a mode"
            )

    @staticmethod
    def _same_identity(left: DeliverySnapshot, right: DeliverySnapshot) -> bool:
        return (
            left.purchase_id,
            left.buff_order_id,
            left.account_id,
            left.recipient_steam_id,
        ) == (
            right.purchase_id,
            right.buff_order_id,
            right.account_id,
            right.recipient_steam_id,
        )

    @staticmethod
    def _snapshot_values(snapshot: DeliverySnapshot) -> tuple[object, ...]:
        return (
            snapshot.purchase_id,
            snapshot.buff_order_id,
            snapshot.account_id,
            snapshot.recipient_steam_id,
            None if snapshot.delivery_mode is None else snapshot.delivery_mode.value,
            snapshot.delivery_status.value,
            snapshot.steam_tradeoffer_id,
            snapshot.offer_attempted_at,
            snapshot.offer_sent_at,
            snapshot.received_at,
            snapshot.delivery_error,
            int(snapshot.pending_receipt),
            snapshot.assetid,
        )

    @classmethod
    def _row_to_stored(cls, row: tuple[object, ...]) -> StoredDelivery:
        if len(row) != 15:
            raise AutoOfferStoreCorruptError("persisted delivery row has wrong shape")
        if type(row[0]) is not int or row[0] < 1:
            raise AutoOfferStoreCorruptError("persisted delivery id is invalid")
        try:
            mode = None if row[5] is None else DeliveryMode(row[5])
            status = DeliveryStatus(row[6])
        except (TypeError, ValueError) as exc:
            raise AutoOfferStoreCorruptError("persisted delivery enum is unknown") from exc
        if type(row[12]) is not int or row[12] not in (0, 1):
            raise AutoOfferStoreCorruptError("persisted pending_receipt is invalid")
        if type(row[14]) is not int or row[14] < 1:
            raise AutoOfferStoreCorruptError("persisted delivery revision is invalid")
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
        )
        try:
            validate_delivery_snapshot(snapshot)
        except DeliveryContractError as exc:
            raise AutoOfferStoreCorruptError(
                "persisted delivery violates the delivery contract"
            ) from exc
        return StoredDelivery(snapshot=snapshot, revision=row[14])


__all__ = [
    "AUTO_OFFER_STORE_SCHEMA_VERSION",
    "AutoOfferStore",
    "AutoOfferStoreConflictError",
    "AutoOfferStoreCorruptError",
    "AutoOfferStoreError",
    "AutoOfferStoreSchemaError",
    "AutoOfferStoreStaleWriteError",
    "StoredDelivery",
]
