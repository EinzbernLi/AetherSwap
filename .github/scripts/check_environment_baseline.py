"""Fail-closed verifier for the repository's reproducible Python baseline."""

from __future__ import annotations

import sys
from importlib import metadata


SUPPORTED_PYTHON_MINOR = (3, 12)
CANONICAL_CI_PYTHON = (3, 12, 13)
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
    actual_minor = tuple(sys.version_info[:2])

    if sys.implementation.name != "cpython":
        failures.append(
            f"python implementation mismatch: expected CPython, got {sys.implementation.name}"
        )
    if actual_minor != SUPPORTED_PYTHON_MINOR:
        failures.append(
            "python minor mismatch: "
            f"expected CPython {'.'.join(map(str, SUPPORTED_PYTHON_MINOR))}.x, "
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
        "(supported CPython",
        ".".join(map(str, SUPPORTED_PYTHON_MINOR)) + ".x; canonical CI",
        ".".join(map(str, CANONICAL_CI_PYTHON)) + ")",
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
