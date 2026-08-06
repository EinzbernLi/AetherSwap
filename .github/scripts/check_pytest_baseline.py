"""Evaluate pytest JUnit XML with a strict, fail-closed baseline gate.

The gate counts testcase result elements rather than trusting aggregate XML
attributes.  Suite attributes are then cross-checked against those testcase
results so malformed or incomplete reports cannot be mistaken for a green
test run.
"""

import sys
import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class BaselineGateError(ValueError):
    """Raised when a JUnit report cannot be safely evaluated."""


@dataclass(frozen=True)
class JUnitStats:
    """Validated testcase and suite statistics from a JUnit report."""

    observed_total: int
    passed: int
    failures: int
    testcase_errors: int
    collection_errors: int
    skipped: int


def _parse_nonnegative_attribute(suite: ET.Element, name: str) -> int:
    value = suite.attrib.get(name)
    if value is None:
        raise BaselineGateError(f"leaf testsuite is missing required attribute: {name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BaselineGateError(f"leaf testsuite attribute {name!r} is not an integer") from exc
    if parsed < 0:
        raise BaselineGateError(f"leaf testsuite attribute {name!r} is negative")
    return parsed


def _classify_testcase(case: ET.Element) -> str:
    result_names = ("failure", "error", "skipped")
    direct_results: list[str] = []
    for child in list(case):
        if child.tag in result_names:
            direct_results.append(child.tag)

    nested_results = [
        child.tag
        for child in case.iter()
        if child is not case and child.tag in result_names and child not in list(case)
    ]
    if nested_results:
        raise BaselineGateError("testcase result marker is not a direct child")
    if len(direct_results) > 1 or len(set(direct_results)) != len(direct_results):
        raise BaselineGateError("testcase contains conflicting or duplicate result markers")
    return direct_results[0] if direct_results else "passed"


def _leaf_suites(root: ET.Element) -> list[ET.Element]:
    if root.tag not in {"testsuite", "testsuites"}:
        raise BaselineGateError(f"unsupported JUnit root: {root.tag!r}")
    if root.tag == "testsuites" and not any(child.tag == "testsuite" for child in list(root)):
        raise BaselineGateError("testsuites root has no testsuite children")

    suites = list(root.iter("testsuite"))
    leaves = [suite for suite in suites if any(child.tag == "testcase" for child in list(suite))]
    if not leaves:
        raise BaselineGateError("JUnit report has no testcase-containing leaf testsuite")

    parent_by_id: dict[int, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parent_by_id[id(child)] = parent
    for case in root.iter("testcase"):
        parent = parent_by_id.get(id(case))
        if parent not in leaves:
            raise BaselineGateError("testcase cannot be attributed to a leaf testsuite")
    return leaves


def evaluate_junit(junit_path: str | Path) -> JUnitStats:
    """Parse and validate a supported JUnit XML report."""
    path = Path(junit_path)
    if not path.exists():
        raise BaselineGateError(f"JUnit file does not exist: {path}")
    if not path.is_file():
        raise BaselineGateError(f"JUnit path is not a file: {path}")
    try:
        if path.stat().st_size == 0:
            raise BaselineGateError("JUnit file is empty")
        root = ET.parse(path).getroot()
    except BaselineGateError:
        raise
    except (OSError, ET.ParseError) as exc:
        raise BaselineGateError(f"cannot parse JUnit XML: {exc}") from exc

    leaves = _leaf_suites(root)
    observed_total = 0
    actual_failures = 0
    actual_errors = 0
    actual_skipped = 0
    suite_tests_total = 0
    suite_failures_total = 0
    suite_errors_total = 0
    suite_skipped_total = 0

    for suite in leaves:
        suite_tests_total += _parse_nonnegative_attribute(suite, "tests")
        suite_failures_total += _parse_nonnegative_attribute(suite, "failures")
        suite_errors_total += _parse_nonnegative_attribute(suite, "errors")
        suite_skipped_total += _parse_nonnegative_attribute(suite, "skipped")
        cases = [child for child in list(suite) if child.tag == "testcase"]
        observed_total += len(cases)
        for case in cases:
            result = _classify_testcase(case)
            if result == "failure":
                actual_failures += 1
            elif result == "error":
                actual_errors += 1
            elif result == "skipped":
                actual_skipped += 1

    collection_errors = suite_errors_total - actual_errors
    if collection_errors < 0:
        raise BaselineGateError("suite errors are fewer than testcase errors")
    if suite_tests_total != observed_total:
        raise BaselineGateError("suite tests metadata does not match testcase count")
    if suite_failures_total != actual_failures:
        raise BaselineGateError("suite failures metadata does not match testcase results")
    if suite_skipped_total != actual_skipped:
        raise BaselineGateError("suite skipped metadata does not match testcase results")
    if suite_errors_total < actual_errors:
        raise BaselineGateError("suite errors metadata does not cover testcase errors")
    if observed_total == 0:
        raise BaselineGateError("JUnit report contains no testcase elements")

    passed = observed_total - actual_failures - actual_errors - actual_skipped
    if passed < 0:
        raise BaselineGateError("JUnit statistics produce a negative passed count")
    return JUnitStats(
        observed_total=observed_total,
        passed=passed,
        failures=actual_failures,
        testcase_errors=actual_errors,
        collection_errors=collection_errors,
        skipped=actual_skipped,
    )


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the TASK-004 pytest baseline gate.")
    parser.add_argument("--junit", required=True, help="path to the pytest JUnit XML report")
    parser.add_argument("--pytest-status", required=True, type=int, help="actual pytest exit status")
    parser.add_argument("--minimum-tests", required=True, type=_positive_integer)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the strict gate and return zero only for a fully green report."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 2
        if code != 0:
            print("Gate result: FAILED")
        return code

    try:
        stats = evaluate_junit(args.junit)
    except BaselineGateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Gate result: FAILED")
        return 1

    print(f"Minimum test baseline: {args.minimum_tests}")
    print(f"Collected test cases: {stats.observed_total}")
    print(f"Passed: {stats.passed}")
    print(f"Failed: {stats.failures}")
    print(f"Errors: {stats.testcase_errors}")
    print(f"Collection errors: {stats.collection_errors}")
    print(f"Skipped: {stats.skipped}")
    print("Registered failures: 0")
    print(f"Pytest exit status: {args.pytest_status}")

    passed = (
        args.pytest_status == 0
        and stats.observed_total >= args.minimum_tests
        and stats.failures == 0
        and stats.testcase_errors == 0
        and stats.collection_errors == 0
        and stats.skipped == 0
    )
    print(f"Gate result: {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
