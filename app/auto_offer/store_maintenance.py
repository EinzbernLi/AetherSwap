"""Explicit, zero-platform-I/O maintenance for existing Auto Offer stores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .store import (
    AutoOfferStore,
    AutoOfferStoreCorruptError,
    AutoOfferStoreError,
    AutoOfferStoreSchemaError,
)


_STORE_PATH = Path(__file__).resolve().parents[2] / "config" / "auto_offer.db"


class StoreMaintenanceBlocked(AutoOfferStoreError):
    """Raised when an explicit Store maintenance transition is unsafe."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class StoreMaintenanceResult:
    """The bounded result of one explicit existing-file maintenance attempt."""

    action: str
    migrated: bool = False


def maintain_existing_store_for_enable(
    db_path: str | Path | None = None,
) -> StoreMaintenanceResult:
    """Migrate a compatible existing v1 Store, or leave missing/v2 sources alone.

    This façade is intentionally callable only by the persisted OFF->ON
    transition path.  It never initializes a Store and never performs platform
    I/O.
    """

    path = _STORE_PATH if db_path is None else Path(db_path)
    try:
        probe = AutoOfferStore.probe_existing_schema(path)
    except AutoOfferStoreSchemaError as exc:
        raise StoreMaintenanceBlocked("store_schema_incompatible") from exc
    except AutoOfferStoreCorruptError as exc:
        raise StoreMaintenanceBlocked("store_migration_incompatible") from exc
    except AutoOfferStoreError as exc:
        raise StoreMaintenanceBlocked("store_unreadable") from exc

    if not probe.exists:
        return StoreMaintenanceResult("missing")
    if probe.version == 2:
        return StoreMaintenanceResult("already_v2")
    if not probe.migratable_v1:
        raise StoreMaintenanceBlocked("store_migration_incompatible")

    try:
        migrated = AutoOfferStore.migrate_existing_v1_to_v2(path)
    except AutoOfferStoreSchemaError as exc:
        raise StoreMaintenanceBlocked("store_migration_incompatible") from exc
    except AutoOfferStoreError as exc:
        raise StoreMaintenanceBlocked("store_migration_blocked") from exc
    if not migrated:
        raise StoreMaintenanceBlocked("store_source_changed")
    return StoreMaintenanceResult("migrated_v1", migrated=True)


__all__ = [
    "StoreMaintenanceBlocked",
    "StoreMaintenanceResult",
    "maintain_existing_store_for_enable",
]
