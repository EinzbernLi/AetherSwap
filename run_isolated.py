"""Explicit launcher for an isolated, tree-local Host process."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_application_main():
    """Bind authority before importing the ordinary Host application."""

    from app.auto_offer.canary_authority import bind_isolated_canary_authority

    bind_isolated_canary_authority(PROJECT_ROOT)
    from app.main import main as application_main

    return application_main


def main() -> None:
    _load_application_main()()


if __name__ == "__main__":
    main()
