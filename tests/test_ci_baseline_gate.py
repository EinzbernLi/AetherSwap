"""Tests for the independent TASK-004 pytest baseline gate."""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "check_pytest_baseline.py"


@pytest.fixture
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pytest_baseline", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_suite(
    parent: ET.Element,
    markers: list[str | tuple[str, ...] | None],
    *,
    collection_errors: int = 0,
    metadata: dict[str, object] | None = None,
) -> ET.Element:
    failure_count = sum(marker == "failure" for marker in markers)
    error_count = sum(marker == "error" for marker in markers)
    skipped_count = sum(marker == "skipped" for marker in markers)
    attrs: dict[str, str] = {
        "tests": str(len(markers)),
        "failures": str(failure_count),
        "errors": str(error_count + collection_errors),
        "skipped": str(skipped_count),
    }
    if metadata:
        attrs.update({key: str(value) for key, value in metadata.items()})
    suite = ET.SubElement(parent, "testsuite", attrs)
    for index, marker in enumerate(markers):
        case = ET.SubElement(suite, "testcase", {"classname": "tests", "name": f"case_{index}"})
        names = [marker] if isinstance(marker, str) else marker
        if names:
            for name in names:
                ET.SubElement(case, name)
    return suite


def _write_report(
    tmp_path: Path,
    markers: list[str | tuple[str, ...] | None] | None = None,
    *,
    root_tag: str = "testsuite",
    collection_errors: int = 0,
    metadata: dict[str, object] | None = None,
    suite_specs: list[tuple[list[str | tuple[str, ...] | None], int, dict[str, object] | None]] | None = None,
) -> Path:
    root = ET.Element(root_tag)
    if suite_specs is not None:
        for suite_markers, suite_collection_errors, suite_metadata in suite_specs:
            _make_suite(
                root,
                suite_markers,
                collection_errors=suite_collection_errors,
                metadata=suite_metadata,
            )
    elif root_tag == "testsuite":
        suite = root
        suite_markers = markers or []
        failure_count = sum(marker == "failure" for marker in suite_markers)
        error_count = sum(marker == "error" for marker in suite_markers)
        skipped_count = sum(marker == "skipped" for marker in suite_markers)
        attrs = {
            "tests": str(len(suite_markers)),
            "failures": str(failure_count),
            "errors": str(error_count + collection_errors),
            "skipped": str(skipped_count),
        }
        if metadata:
            attrs.update({key: str(value) for key, value in metadata.items()})
        suite.attrib.update(attrs)
        for index, marker in enumerate(suite_markers):
            case = ET.SubElement(suite, "testcase", {"classname": "tests", "name": f"case_{index}"})
            names = [marker] if isinstance(marker, str) else marker
            if names:
                for name in names:
                    ET.SubElement(case, name)
    elif root_tag == "testsuites":
        _make_suite(root, markers or [], collection_errors=collection_errors, metadata=metadata)

    path = tmp_path / "pytest.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _args(path: Path, *, status: object = 0, minimum: object = 447) -> list[str]:
    return ["--junit", str(path), "--pytest-status", str(status), "--minimum-tests", str(minimum)]


def test_447_all_pass(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report)) == 0


def test_448_all_pass(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None] * 448)
    assert gate.main(_args(report)) == 0


def test_449_all_pass(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None] * 449)
    assert gate.main(_args(report)) == 0


def test_446_all_pass_is_below_baseline(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None] * 446)
    assert gate.main(_args(report)) != 0


@pytest.mark.parametrize("marker", ["failure", "error", "skipped"])
def test_result_marker_blocks_gate(gate: ModuleType, tmp_path: Path, marker: str) -> None:
    report = _write_report(tmp_path, [None] * 446 + [marker])
    assert gate.main(_args(report)) != 0


@pytest.mark.parametrize("status", [1, 2, 17, -1])
def test_any_nonzero_pytest_status_blocks_gate(gate: ModuleType, tmp_path: Path, status: int) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report, status=status)) != 0


def test_missing_junit_file_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    assert gate.main(_args(tmp_path / "missing.xml")) != 0


def test_empty_junit_file_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = tmp_path / "empty.xml"
    report.write_text("", encoding="utf-8")
    assert gate.main(_args(report)) != 0


def test_corrupt_junit_xml_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = tmp_path / "corrupt.xml"
    report.write_text("<testsuite>", encoding="utf-8")
    assert gate.main(_args(report)) != 0


def test_unsupported_root_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None], root_tag="unsupported")
    assert gate.main(_args(report, minimum=1)) != 0


@pytest.mark.parametrize("minimum", [0, -1, "not-an-integer"])
def test_invalid_minimum_tests_blocks_gate(gate: ModuleType, tmp_path: Path, minimum: object) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report, minimum=minimum)) != 0


def test_non_integer_pytest_status_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report, status="not-an-integer")) != 0


@pytest.mark.parametrize("attribute", ["tests", "failures", "errors", "skipped"])
def test_missing_suite_metadata_blocks_gate(gate: ModuleType, tmp_path: Path, attribute: str) -> None:
    report = _write_report(tmp_path, [None], metadata={attribute: ""})
    tree = ET.parse(report)
    del tree.getroot().attrib[attribute]
    tree.write(report, encoding="utf-8", xml_declaration=True)
    assert gate.main(_args(report, minimum=1)) != 0


@pytest.mark.parametrize("attribute", ["tests", "failures", "errors", "skipped"])
@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_invalid_suite_metadata_blocks_gate(
    gate: ModuleType, tmp_path: Path, attribute: str, value: str
) -> None:
    report = _write_report(tmp_path, [None], metadata={attribute: value})
    assert gate.main(_args(report, minimum=1)) != 0


def test_tests_metadata_mismatch_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, [None], metadata={"tests": 2})
    assert gate.main(_args(report, minimum=1)) != 0


def test_failures_metadata_mismatch_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, ["failure"], metadata={"failures": 0})
    assert gate.main(_args(report, minimum=1)) != 0


def test_skipped_metadata_mismatch_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, ["skipped"], metadata={"skipped": 0})
    assert gate.main(_args(report, minimum=1)) != 0


def test_errors_metadata_less_than_testcase_errors_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, ["error"], metadata={"errors": 0})
    assert gate.main(_args(report, minimum=1)) != 0


@pytest.mark.parametrize(
    "markers",
    [["failure", "error"], ["failure", "skipped"], ["error", "skipped"]],
)
def test_conflicting_testcase_results_block_gate(
    gate: ModuleType,
    tmp_path: Path,
    markers: list[str],
) -> None:
    report = _write_report(tmp_path, [tuple(markers)])
    assert gate.main(_args(report, minimum=1)) != 0


def test_multiple_leaf_testsuites_are_aggregated(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        suite_specs=[([None] * 224, 0, None), ([None] * 223, 0, None)],
        root_tag="testsuites",
    )
    assert gate.main(_args(report)) == 0


def test_metadata_error_in_one_leaf_blocks_aggregate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        suite_specs=[([None] * 224, 0, None), ([None] * 223, 0, {"tests": 222})],
        root_tag="testsuites",
    )
    assert gate.main(_args(report)) != 0


def test_collection_errors_are_computed_from_suite_error_surplus(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_report(tmp_path, [None] * 447, collection_errors=2)
    assert gate.main(_args(report)) != 0
    output = capsys.readouterr().out
    assert "Collection errors: 2" in output
    assert "Gate result: FAILED" in output


def test_testsuites_without_leaf_testsuite_blocks_gate(gate: ModuleType, tmp_path: Path) -> None:
    report = _write_report(tmp_path, root_tag="testsuites", suite_specs=[])
    assert gate.main(_args(report)) != 0


def test_no_failure_exemption_identifiers_in_gate_source() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8").lower()
    forbidden = (
        "expected_failures",
        "allowed_failures",
        "known_failures",
        "registered_failure_nodes",
        "failure whitelist",
        "failure allowlist",
    )
    assert all(token not in source for token in forbidden)


def test_output_always_reports_zero_registered_failures(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report)) == 0
    assert "Registered failures: 0" in capsys.readouterr().out


def test_success_output_reports_passed(gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _write_report(tmp_path, [None] * 447)
    assert gate.main(_args(report)) == 0
    assert "Gate result: PASSED" in capsys.readouterr().out


def test_failure_output_reports_failed(gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _write_report(tmp_path, [None] * 446)
    assert gate.main(_args(report)) != 0
    output = capsys.readouterr().out
    assert "Gate result: FAILED" in output
    assert "Gate result: PASSED" not in output


@pytest.mark.parametrize(
    "arguments",
    [[], ["--junit"], ["--pytest-status", "0"], ["--minimum-tests", "447"]],
)
def test_required_cli_arguments_are_explicit(gate: ModuleType, arguments: list[str]) -> None:
    assert gate.main(arguments) != 0
