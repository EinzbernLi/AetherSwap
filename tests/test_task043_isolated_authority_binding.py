from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_probe(source: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe failed with {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_default_authority_remains_lazy_production_authority():
    _run_probe(
        """
        import app.auto_offer.canary_authority as module

        authority = module.get_canary_authority()
        assert authority._root == module._production_root()
        assert authority._host_db_path == module._PRODUCTION_HOST_DB_PATH
        assert module.get_canary_authority() is authority
        """
    )


def test_isolated_binding_uses_exact_tree_paths_without_creation_or_io():
    _run_probe(
        """
        import builtins
        import os
        import tempfile
        from pathlib import Path

        import app.auto_offer.canary_authority as module

        def fail(*_args, **_kwargs):
            raise AssertionError("isolated binding performed filesystem I/O")

        Path.mkdir = fail
        builtins.open = fail
        os.mkdir = fail
        os.makedirs = fail
        os.open = fail
        os.replace = fail
        tempfile.mkstemp = fail

        root = Path(module.__file__).resolve().parents[2]
        authority = module.bind_isolated_canary_authority(root)
        assert authority is module.get_canary_authority()
        assert authority._root == root / ".aetherswap"
        assert authority._host_db_path == root / "config" / "app.db"
        """
    )


def test_isolated_binding_rejects_root_mismatch():
    _run_probe(
        """
        from pathlib import Path

        import app.auto_offer.canary_authority as module

        root = Path(module.__file__).resolve().parents[2]
        try:
            module.bind_isolated_canary_authority(root / "not-the-source-root")
        except module.CanaryAuthorityError as exc:
            assert str(exc) == "isolated_project_root_mismatch"
        else:
            raise AssertionError("root mismatch was accepted")
        """
    )


def test_isolated_binding_rejects_second_binding():
    _run_probe(
        """
        from pathlib import Path

        import app.auto_offer.canary_authority as module

        root = Path(module.__file__).resolve().parents[2]
        first = module.bind_isolated_canary_authority(root)
        try:
            module.bind_isolated_canary_authority(root)
        except module.CanaryAuthorityError as exc:
            assert str(exc) == "canary_authority_already_resolved"
        else:
            raise AssertionError("second binding was accepted")
        assert module.get_canary_authority() is first
        """
    )


def test_isolated_binding_rejects_prior_default_resolution():
    _run_probe(
        """
        from pathlib import Path

        import app.auto_offer.canary_authority as module

        module.get_canary_authority()
        root = Path(module.__file__).resolve().parents[2]
        try:
            module.bind_isolated_canary_authority(root)
        except module.CanaryAuthorityError as exc:
            assert str(exc) == "canary_authority_already_resolved"
        else:
            raise AssertionError("binding after resolution was accepted")
        """
    )


def test_bound_authority_is_visible_to_ordinary_host_consumers():
    _run_probe(
        """
        from pathlib import Path

        import app.auto_offer.canary_authority as module
        import app.auto_offer.host_integration as host_integration

        root = Path(module.__file__).resolve().parents[2]
        authority = module.bind_isolated_canary_authority(root)
        assert host_integration.get_canary_authority() is authority
        assert module.get_canary_authority() is authority
        """
    )


def test_isolated_launcher_binds_before_application_import():
    _run_probe(
        """
        import builtins
        import sys

        import run_isolated

        imported_api = []
        original_import = builtins.__import__

        def tracked_import(name, *args, **kwargs):
            if name == "app.api":
                imported_api.append(name)
            return original_import(name, *args, **kwargs)

        builtins.__import__ = tracked_import
        application_main = run_isolated._load_application_main()
        assert application_main.__module__ == "app.main"
        assert not imported_api
        assert "app.api" not in sys.modules
        from app.auto_offer.canary_authority import get_canary_authority

        assert get_canary_authority()._root == run_isolated.PROJECT_ROOT / ".aetherswap"
        """
    )
