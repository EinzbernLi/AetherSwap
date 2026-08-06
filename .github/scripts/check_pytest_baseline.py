"""Fail-closed skeleton for the TASK-004 pytest baseline gate.

The implementation is intentionally incomplete at Checkpoint 0.  It must not
be connected to CI or treated as a passing gate until the JUnit evaluator and
its tests are implemented in later checkpoints.
"""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Report that the gate is not implemented and fail closed."""
    del argv
    print("TASK-004 baseline gate skeleton is incomplete; failing closed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
