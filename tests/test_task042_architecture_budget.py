from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
AUTO_OFFER_ROOT = APP_ROOT / "auto_offer"
_FORBIDDEN_INTERNAL_MODULES = {
    "app.auto_offer.store",
    "app.auto_offer.coordinator",
}


def _python_files(root: Path):
    yield from sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def test_host_never_imports_store_or_coordinator_internals_directly():
    """Keep Store/Coordinator private to the Auto Offer façade.

    Host integration may import public Auto Offer façade/contracts, but state
    authority internals must not leak across more Host files over time.
    """

    violations = []
    for path in _python_files(APP_ROOT):
        if AUTO_OFFER_ROOT in path.parents:
            continue
        for module in _imports(path):
            if module in _FORBIDDEN_INTERNAL_MODULES or any(
                module.startswith(prefix + ".")
                for prefix in _FORBIDDEN_INTERNAL_MODULES
            ):
                violations.append((path.relative_to(ROOT).as_posix(), module))

    assert violations == []


def test_auto_offer_has_no_background_scheduler_entrypoint():
    """The module must remain tick-driven by the existing Host worker."""

    forbidden_calls = {
        "threading.Thread",
        "asyncio.create_task",
        "asyncio.ensure_future",
    }
    violations = []
    for path in _python_files(AUTO_OFFER_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = f"{func.value.id}.{func.attr}"
                if name in forbidden_calls:
                    violations.append((path.relative_to(ROOT).as_posix(), name))

    assert violations == []
