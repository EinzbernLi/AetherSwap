"""Global pytest isolation for host runtime state.

The production defaults remain unchanged. During tests only, host database and
BUFF checkout-guard paths are redirected to pytest-managed temporary storage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_host_runtime_state(tmp_path_factory: pytest.TempPathFactory):
    """Keep full-suite runtime artifacts outside the source workspace."""
    from app import database
    from app.auto_offer import host_integration
    from app.services import buff_checkout_guard

    state_dir = tmp_path_factory.mktemp("aetherswap-host-state")
    patch = pytest.MonkeyPatch()
    old_engine = database._engine
    if old_engine is not None:
        old_engine.dispose()

    patch.setattr(database, "_CONFIG_DIR", state_dir)
    patch.setattr(database, "_DB_PATH", state_dir / "app.db")
    patch.setattr(database, "_TRANSACTIONS_JSON", state_dir / "transactions.json")
    patch.setattr(database, "_TRANSACTIONS_BAK", state_dir / "transactions.json.bak")
    patch.setattr(database, "_engine", None)
    patch.setattr(
        buff_checkout_guard,
        "_GUARD_PATH",
        Path(state_dir) / "buff_checkout_guard.json",
    )
    # H2 removed raw requests.Session ownership from host_integration. A few
    # legacy tests still monkeypatch the old attribute during setup. Preserve
    # only that pytest-time patch target until those large legacy suites are
    # naturally migrated; production builders do not read this namespace.
    patch.setattr(
        host_integration,
        "requests",
        SimpleNamespace(Session=None),
        raising=False,
    )
    database.init_db()
    try:
        yield
    finally:
        active_engine = database._engine
        if active_engine is not None:
            active_engine.dispose()
        patch.undo()
