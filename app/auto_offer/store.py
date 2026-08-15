"""Independent, fail-closed SQLite persistence for native Auto Offer state."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator

from .contracts import (
    DeliveryContractError,
    DeliveryMode,
    DeliverySnapshot,
    DeliveryStatus,
    validate_delivery_snapshot,
    validate_delivery_transition,
)


AUTO_OFFER_STORE_SCHEMA_VERSION: Final[int] = 2
_TABLE_NAME: Final[str] = "auto_offer_delivery"
_SOURCE_SUFFIXES: Final[tuple[str, ...]] = ("", "-wal", "-shm", "-journal")
_RECOVERABLE_STATUSES: Final[frozenset[DeliveryStatus]] = frozenset(
    {
        DeliveryStatus.PENDING_DIRECTION,
        DeliveryStatus.AWAITING_OFFER,
        DeliveryStatus.OFFER_ATTEMPTED,
        DeliveryStatus.OFFER_SENT,
        DeliveryStatus.OFFER_CONFIRMATION_REQUIRED,
        DeliveryStatus.OFFER_CONFIRMATION_ATTEMPTED,
        DeliveryStatus.OFFER_RECEIVED,
        DeliveryStatus.OFFER_CONFIRMED,
        DeliveryStatus.OFFER_ACCEPT_ATTEMPTED,
        DeliveryStatus.AWAITING_INVENTORY,
        DeliveryStatus.OFFER_TERMINATED,
        DeliveryStatus.REFUND_CLEANUP_PENDING,
        DeliveryStatus.RESULT_UNKNOWN,
    }
)
_SELECT_COLUMNS: Final[str] = (
    "id, purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, counterparty_steam_id, revision"
)
_V1_SELECT_COLUMNS: Final[str] = (
    "id, purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, revision"
)
_INSERT_COLUMNS: Final[str] = (
    "purchase_id, buff_order_id, account_id, recipient_steam_id, "
    "delivery_mode, delivery_status, steam_tradeoffer_id, "
    "offer_attempted_at, offer_sent_at, received_at, delivery_error, "
    "pending_receipt, assetid, counterparty_steam_id, revision"
)
_V1_EXPECTED_COLUMNS: Final[tuple[tuple[str, str, int, int], ...]] = (
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
_EXPECTED_COLUMNS: Final[tuple[tuple[str, str, int, int], ...]] = (
    *_V1_EXPECTED_COLUMNS,
    ("counterparty_steam_id", "TEXT", 0, 0),
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
    revision INTEGER NOT NULL,
    counterparty_steam_id TEXT NULL
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


@dataclass(frozen=True)
class StoreSchemaProbe:
    """Read-only evidence about an existing Auto Offer Store source."""

    exists: bool
    version: int | None
    migratable_v1: bool


@dataclass(frozen=True)
class _SourceFingerprint:
    exists: bool
    device: int | None
    inode: int | None
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    sha256: str | None


@dataclass(frozen=True)
class _DetachedSource:
    main: bytes
    wal: bytes | None
    fingerprint: tuple[_SourceFingerprint, ...]


class AutoOfferStore:
    """A small explicit API around the independent Auto Offer database.

    Construction is intentionally side-effect free.  The caller must invoke
    :meth:`initialize` before any database operation.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open the database, configure SQLite, and create or verify current v2."""
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
            version = self._user_version(connection)
            tables = self._user_tables(connection)
            if version != 0 or tables:
                self._validate_schema(connection)
                self._configure_connection(connection)
            else:
                self._configure_connection(connection)
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

    @classmethod
    def inspect_existing_by_buff_order_id(
        cls,
        db_path: str | Path,
        buff_order_id: str,
    ) -> StoredDelivery | None:
        """Read one current v2 row without creating, migrating, or writing Store state."""
        if (
            type(buff_order_id) is not str
            or not buff_order_id
            or buff_order_id.strip() != buff_order_id
            or any(ord(character) < 32 for character in buff_order_id)
        ):
            raise AutoOfferStoreError("buff_order_id must be a non-whitespace ID")
        with cls._detached_readonly(db_path) as connection:
            if connection is None:
                return None
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} WHERE buff_order_id = ?",
                (buff_order_id,),
            ).fetchall()
            if len(rows) > 1:
                raise AutoOfferStoreCorruptError(
                    "buff_order_id maps to multiple persisted rows"
                )
            return None if not rows else cls._row_to_stored(rows[0])


    @classmethod
    def inspect_existing(cls, db_path: str | Path) -> list[StoredDelivery]:
        """Read every current v2 row without initializing or mutating the Store."""
        with cls._detached_readonly(db_path) as connection:
            if connection is None:
                return []
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} ORDER BY id ASC"
            ).fetchall()
            return [cls._row_to_stored(row) for row in rows]

    @classmethod
    def probe_existing_schema(cls, db_path: str | Path) -> StoreSchemaProbe:
        """Inspect an existing source without creating, migrating, or writing it."""

        with cls._detached_readonly(db_path, require_v2=False) as connection:
            if connection is None:
                return StoreSchemaProbe(False, None, False)
            version = cls._user_version(connection)
            if version == 1:
                cls._validate_v1_schema(connection)
                cls._validate_v1_rows(connection)
                return StoreSchemaProbe(True, 1, True)
            if version == AUTO_OFFER_STORE_SCHEMA_VERSION:
                cls._validate_schema(connection)
                return StoreSchemaProbe(True, version, False)
            raise AutoOfferStoreSchemaError("unsupported Auto Offer schema version")

    @classmethod
    def migrate_existing_v1_to_v2(cls, db_path: str | Path) -> bool:
        """Migrate one already-existing compatible v1 source without creation.

        Detached evidence approves one exact logical v1 snapshot.  The live
        source is then opened existing-file-only, write custody is acquired,
        and both physical main-file identity and the ordered logical v1 rows
        are re-proven inside that transaction before any schema mutation.
        """

        source = cls._capture_source(db_path)
        if source is None:
            return False

        with cls._detached_readonly(db_path, require_v2=False) as connection:
            if connection is None:
                return False
            if cls._user_version(connection) != 1:
                cls._validate_schema(connection)
                return False
            cls._validate_v1_schema(connection)
            approved_rows = cls._validate_v1_rows(connection)

        approved_main = source.fingerprint[0]
        if (
            not approved_main.exists
            or approved_main.device is None
            or approved_main.inode is None
        ):
            raise AutoOfferStoreError(
                "cannot prove existing Auto Offer source identity"
            )

        path = cls._resolved_source_path(db_path)
        current = cls._capture_source(path)
        if current != source:
            raise AutoOfferStoreError("Auto Offer source changed before migration")

        connection = cls._open_existing_rw(path)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            if connection.execute("PRAGMA busy_timeout").fetchone()[0] != 5000:
                raise AutoOfferStoreCorruptError(
                    "SQLite busy timeout was not enabled"
                )
            connection.execute("PRAGMA synchronous = FULL")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise AutoOfferStoreCorruptError(
                    "SQLite FULL synchronous mode was not enabled"
                )

            cls._begin(connection)
            try:
                cls._assert_main_source_approval(path, approved_main)
                cls._validate_v1_schema(connection)
                live_rows = cls._validate_v1_rows(connection)
                if live_rows != approved_rows:
                    raise AutoOfferStoreError(
                        "Auto Offer logical source changed before migration"
                    )

                cls._apply_v1_to_v2_schema_change(connection)
                cls._validate_schema(connection)

                # A path replacement after write custody but before commit must
                # still abort while ALTER/user_version remain rollback-able.
                cls._assert_main_source_identity(path, approved_main)
                connection.commit()
            except (AutoOfferStoreError, sqlite3.DatabaseError):
                cls._rollback(connection)
                raise
            return True
        except AutoOfferStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            cls._rollback(connection)
            raise AutoOfferStoreCorruptError(
                "SQLite rejected Auto Offer schema migration"
            ) from exc
        finally:
            try:
                connection.close()
            except sqlite3.DatabaseError as exc:
                raise AutoOfferStoreError("cannot close Auto Offer migration connection") from exc

    @classmethod
    @contextmanager
    def _detached_readonly(
        cls,
        db_path: str | Path,
        *,
        require_v2: bool = True,
    ) -> Iterator[sqlite3.Connection | None]:
        source = cls._capture_source(db_path)
        if source is None:
            yield None
            return

        temporary = tempfile.TemporaryDirectory(prefix="aetherswap-auto-offer-")
        detached_path = Path(temporary.name) / "auto_offer.db"
        try:
            detached_path.write_bytes(source.main)
            if source.wal is not None:
                Path(f"{detached_path}-wal").write_bytes(source.wal)
            connection = sqlite3.connect(
                str(detached_path),
                timeout=5.0,
                isolation_level=None,
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            temporary.cleanup()
            raise AutoOfferStoreError("cannot open detached Auto Offer database") from exc
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise AutoOfferStoreCorruptError("SQLite query_only was not enabled")
            connection.execute("PRAGMA trusted_schema = OFF")
            trusted = connection.execute("PRAGMA trusted_schema").fetchone()
            if trusted is None or trusted[0] != 0:
                raise AutoOfferStoreCorruptError("SQLite trusted_schema was not disabled")
            connection.execute("PRAGMA busy_timeout = 5000")
            if connection.execute("PRAGMA busy_timeout").fetchone()[0] != 5000:
                raise AutoOfferStoreCorruptError("SQLite busy timeout was not enabled")
            if require_v2:
                cls._validate_schema(connection)
            if require_v2 and cls._user_version(connection) != AUTO_OFFER_STORE_SCHEMA_VERSION:
                raise AutoOfferStoreSchemaError("Auto Offer schema does not match v2")
            yield connection
        except AutoOfferStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError(
                "SQLite rejected read-only Auto Offer inspection"
            ) from exc
        finally:
            try:
                connection.close()
            except sqlite3.DatabaseError as exc:
                raise AutoOfferStoreError("cannot close detached Auto Offer database") from exc
            temporary.cleanup()

    @classmethod
    def _capture_source(cls, db_path: str | Path) -> _DetachedSource | None:
        path = cls._resolved_source_path(db_path)
        before = cls._fingerprint_source_family(path)
        main, wal, shm, journal = before
        if not main.exists:
            if any(item.exists for item in (wal, shm, journal)):
                raise AutoOfferStoreCorruptError("orphan Auto Offer SQLite sidecar")
            return None
        if journal.exists:
            raise AutoOfferStoreCorruptError("Auto Offer SQLite rollback journal is present")

        main_payload = cls._read_source_bytes(path)
        wal_payload = None
        if wal.exists:
            wal_payload = cls._read_source_bytes(Path(f"{path}-wal"))

        if main_payload is None or hashlib.sha256(main_payload).hexdigest() != main.sha256:
            raise AutoOfferStoreError("Auto Offer source changed during collection")
        if wal.exists and (
            wal_payload is None or hashlib.sha256(wal_payload).hexdigest() != wal.sha256
        ):
            raise AutoOfferStoreError("Auto Offer source changed during collection")

        after = cls._fingerprint_source_family(path)
        if before != after:
            raise AutoOfferStoreError("Auto Offer source changed during collection")
        return _DetachedSource(
            main=main_payload,
            wal=wal_payload,
            fingerprint=before,
        )

    @staticmethod
    def _resolved_source_path(db_path: str | Path) -> Path:
        path = Path(db_path).expanduser()
        try:
            if path.is_symlink():
                raise AutoOfferStoreError("invalid Auto Offer source file")
            path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AutoOfferStoreError("cannot resolve Auto Offer source path") from exc

        return path

    @classmethod
    def _fingerprint_source_family(
        cls,
        path: Path,
    ) -> tuple[_SourceFingerprint, ...]:
        fingerprints: list[_SourceFingerprint] = []
        for suffix in _SOURCE_SUFFIXES:
            candidate = path if not suffix else Path(f"{path}{suffix}")
            try:
                if candidate.is_symlink():
                    raise AutoOfferStoreError("invalid Auto Offer source file")
                if not candidate.exists():
                    fingerprints.append(_SourceFingerprint(False, None, None, None, None, None, None))
                    continue
                if not candidate.is_file():
                    raise AutoOfferStoreError("invalid Auto Offer source file")
                stat = candidate.stat()
                fingerprints.append(
                    _SourceFingerprint(
                        True,
                        getattr(stat, "st_dev", None),
                        getattr(stat, "st_ino", None),
                        stat.st_size,
                        stat.st_mtime_ns,
                        getattr(stat, "st_ctime_ns", None),
                        cls._hash_source_file(candidate),
                    )
                )
            except AutoOfferStoreError:
                raise
            except OSError as exc:
                raise AutoOfferStoreError("cannot fingerprint Auto Offer source") from exc
        return tuple(fingerprints)

    @staticmethod
    def _hash_source_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise AutoOfferStoreError("cannot read Auto Offer source") from exc
        return digest.hexdigest()

    @staticmethod
    def _read_source_bytes(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise AutoOfferStoreError("cannot read Auto Offer source") from exc

    def ensure_initial(self, snapshot: DeliverySnapshot) -> StoredDelivery:
        """Insert one pending-direction delivery, or return its exact duplicate."""

        stored, _created = self.ensure_initial_with_created(snapshot)
        return stored

    def ensure_initial_with_created(
        self,
        snapshot: DeliverySnapshot,
    ) -> tuple[StoredDelivery, bool]:
        """Atomically return the initial row and whether this call inserted it."""

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
                return existing, False

            connection.execute(
                f"INSERT INTO {_TABLE_NAME} ({_INSERT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._snapshot_values(snapshot) + (1,),
            )
            connection.commit()
            return StoredDelivery(snapshot=snapshot, revision=1), True
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
        if (
            current.snapshot.counterparty_steam_id is not None
            and target.counterparty_steam_id
            != current.snapshot.counterparty_steam_id
        ):
            raise DeliveryContractError("bound counterparty Steam ID cannot change")

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
                "counterparty_steam_id = ?, "
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
            raise AutoOfferStoreSchemaError("Auto Offer schema does not match v2")

        cls._validate_table_shape(connection, _EXPECTED_COLUMNS, "v2")

    @classmethod
    def _validate_v1_schema(cls, connection: sqlite3.Connection) -> None:
        if cls._user_version(connection) != 1 or cls._user_tables(connection) != {
            _TABLE_NAME
        }:
            raise AutoOfferStoreSchemaError("Auto Offer schema does not match v1")
        cls._validate_table_shape(connection, _V1_EXPECTED_COLUMNS, "v1")

    @classmethod
    def _validate_v1_rows(
        cls,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[object, ...], ...]:
        try:
            rows = tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT {_V1_SELECT_COLUMNS} FROM {_TABLE_NAME} ORDER BY id ASC"
                ).fetchall()
            )
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError(
                "cannot inspect v1 Auto Offer rows"
            ) from exc
        for row in rows:
            cls._row_to_stored(tuple(row[:14]) + (None, row[14]))
        return rows

    @classmethod
    def _validate_table_shape(
        cls,
        connection: sqlite3.Connection,
        expected_columns: tuple[tuple[str, str, int, int], ...],
        version_label: str,
    ) -> None:

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
            unexpected_objects = connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' "
                "AND NOT (type = 'table' AND name = ?)",
                (_TABLE_NAME,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AutoOfferStoreCorruptError("cannot inspect Auto Offer schema") from exc

        actual_columns = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5]) for row in table_info
        )
        if actual_columns != expected_columns:
            raise AutoOfferStoreSchemaError(
                f"Auto Offer table columns do not match {version_label}"
            )
        if not table_sql or "AUTOINCREMENT" not in str(table_sql[0]).upper():
            raise AutoOfferStoreSchemaError("Auto Offer id must use AUTOINCREMENT")
        if version_label == "v1" and unexpected_objects:
            raise AutoOfferStoreSchemaError(
                f"unexpected Auto Offer schema objects in {version_label}"
            )
        if len(indexes) != 2 or any(index[2] != 1 for index in indexes):
            raise AutoOfferStoreSchemaError(
                f"Auto Offer indexes do not match {version_label}"
            )

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
    def _open_existing_rw(path: Path) -> sqlite3.Connection:
        try:
            return sqlite3.connect(
                f"{path.as_uri()}?mode=rw",
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            raise AutoOfferStoreError(
                "cannot open existing Auto Offer source"
            ) from exc

    @classmethod
    def _assert_main_source_approval(
        cls,
        path: Path,
        approved: _SourceFingerprint,
    ) -> None:
        if (
            not approved.exists
            or approved.device is None
            or approved.inode is None
            or approved.size is None
            or approved.mtime_ns is None
            or approved.ctime_ns is None
            or approved.sha256 is None
        ):
            raise AutoOfferStoreError(
                "cannot prove existing Auto Offer source identity"
            )

        try:
            if path.is_symlink() or not path.exists() or not path.is_file():
                raise AutoOfferStoreError(
                    "Auto Offer source identity changed before migration"
                )
            before = path.stat()
            sha256 = cls._hash_source_file(path)
            after = path.stat()
        except AutoOfferStoreError:
            raise
        except OSError as exc:
            raise AutoOfferStoreError(
                "cannot verify Auto Offer source identity"
            ) from exc

        before_identity = (
            getattr(before, "st_dev", None),
            getattr(before, "st_ino", None),
            before.st_size,
            before.st_mtime_ns,
            getattr(before, "st_ctime_ns", None),
        )
        after_identity = (
            getattr(after, "st_dev", None),
            getattr(after, "st_ino", None),
            after.st_size,
            after.st_mtime_ns,
            getattr(after, "st_ctime_ns", None),
        )
        approved_identity = (
            approved.device,
            approved.inode,
            approved.size,
            approved.mtime_ns,
            approved.ctime_ns,
        )

        if before_identity != after_identity or after_identity != approved_identity:
            raise AutoOfferStoreError(
                "Auto Offer source identity changed before migration"
            )
        if sha256 != approved.sha256:
            raise AutoOfferStoreError(
                "Auto Offer source identity changed before migration"
            )

    @staticmethod
    def _assert_main_source_identity(
        path: Path,
        approved: _SourceFingerprint,
    ) -> None:
        if (
            not approved.exists
            or approved.device is None
            or approved.inode is None
        ):
            raise AutoOfferStoreError(
                "cannot prove existing Auto Offer source identity"
            )
        try:
            if path.is_symlink() or not path.exists() or not path.is_file():
                raise AutoOfferStoreError(
                    "Auto Offer source identity changed before migration"
                )
            stat = path.stat()
        except AutoOfferStoreError:
            raise
        except OSError as exc:
            raise AutoOfferStoreError(
                "cannot verify Auto Offer source identity"
            ) from exc

        if (
            getattr(stat, "st_dev", None),
            getattr(stat, "st_ino", None),
        ) != (approved.device, approved.inode):
            raise AutoOfferStoreError(
                "Auto Offer source identity changed before migration"
            )

    @staticmethod
    def _apply_v1_to_v2_schema_change(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"ALTER TABLE {_TABLE_NAME} "
            "ADD COLUMN counterparty_steam_id TEXT NULL"
        )
        connection.execute(
            f"PRAGMA user_version = {AUTO_OFFER_STORE_SCHEMA_VERSION}"
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
            snapshot.counterparty_steam_id,
        )

    @classmethod
    def _row_to_stored(cls, row: tuple[object, ...]) -> StoredDelivery:
        if len(row) != 16:
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
        if type(row[15]) is not int or row[15] < 1:
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
            counterparty_steam_id=row[14],
        )
        try:
            validate_delivery_snapshot(snapshot)
        except DeliveryContractError as exc:
            raise AutoOfferStoreCorruptError(
                "persisted delivery violates the delivery contract"
            ) from exc
        return StoredDelivery(snapshot=snapshot, revision=row[15])


__all__ = [
    "AUTO_OFFER_STORE_SCHEMA_VERSION",
    "AutoOfferStore",
    "AutoOfferStoreConflictError",
    "AutoOfferStoreCorruptError",
    "AutoOfferStoreError",
    "AutoOfferStoreSchemaError",
    "AutoOfferStoreStaleWriteError",
    "StoreSchemaProbe",
    "StoredDelivery",
]
