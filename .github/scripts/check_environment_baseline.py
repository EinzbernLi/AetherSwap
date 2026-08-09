"""Fail-closed verifier for the repository's reproducible Python baseline."""

from __future__ import annotations

import sys
from importlib import metadata


EXPECTED_PYTHON = (3, 12, 13)
EXPECTED_PACKAGES = {
    "fastapi": "0.141.1",
    "starlette": "1.6.0",
    "pytest": "9.1.1",
    "pydantic": "2.13.4",
    "sqlmodel": "0.0.39",
    "SQLAlchemy": "2.0.51",
    "requests": "2.34.2",
    "urllib3": "2.7.0",
}


def main() -> int:
    failures: list[str] = []
    actual_python = tuple(sys.version_info[:3])
    if actual_python != EXPECTED_PYTHON:
        failures.append(
            "python version mismatch: "
            f"expected {'.'.join(map(str, EXPECTED_PYTHON))}, "
            f"got {'.'.join(map(str, actual_python))}"
        )

    observed: dict[str, str] = {}
    for package, expected in EXPECTED_PACKAGES.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            failures.append(f"missing package: {package}=={expected}")
            continue
        observed[package] = actual
        if actual != expected:
            failures.append(
                f"package version mismatch: {package} expected {expected}, got {actual}"
            )

    print(
        "Python baseline:",
        ".".join(map(str, actual_python)),
        "(expected",
        ".".join(map(str, EXPECTED_PYTHON)) + ")",
    )
    for package in EXPECTED_PACKAGES:
        actual = observed.get(package, "MISSING")
        print(f"{package}: {actual} (expected {EXPECTED_PACKAGES[package]})")

    if failures:
        for failure in failures:
            print(f"ENVIRONMENT_BASELINE_FAIL: {failure}", file=sys.stderr)
        return 1

    print("Environment baseline: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
