import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import NotRequired, TypedDict

import pytest

from pluginbench import assessment_metrics
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.coverage_report import (
    parse_coverage,
    parse_lcov,
    lines_to_numbits,
    numbits_to_lines,
)


class PythonFileData(TypedDict):
    executed_lines: list[int]
    missing_lines: list[int]
    excluded_lines: list[int]
    summary: dict[str, int]
    executed_branches: NotRequired[list[list[int]]]
    missing_branches: NotRequired[list[list[int]]]
    statement_lines: NotRequired[list[int]]


class PythonCoverageData(TypedDict):
    meta: dict[str, object]
    files: dict[str, PythonFileData]
    per_test_numbits: NotRequired[dict[str, dict[str, str]]]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src/index.ts").write_text("export function value(x) {\nreturn x ? 1 : 0;\n}\n")
    return root


def read_report(tmp_path: Path, source: Path, content: str) -> dict[str, object]:
    report = tmp_path / "lcov.info"
    report.write_text(content)
    return parse_lcov(
        report,
        snapshot=source,
        source_scope=["src/**"],
        test_scope=["test/**"],
        workdir="/app",
    )


REPORT = """TN:
SF:/app/src/index.ts
FN:1,value
FNDA:2,value
FNF:1
FNH:1
BRDA:2,0,0,2
BRDA:2,0,1,-
BRF:2
BRH:1
DA:1,1
DA:2,2
LF:2
LH:2
end_of_record
"""


def test_source_bound_coordinates_and_unexecuted_branches(tmp_path: Path, source: Path) -> None:
    report = read_report(tmp_path, source, REPORT)
    assert report["functions"] == {
        "total": ["src/index.ts:1:value"],
        "covered": ["src/index.ts:1:value"],
        "unit": "function",
        "files_measured": ["src/index.ts"],
    }
    assert report["branches"] == {
        "total": ["src/index.ts:2:0:0", "src/index.ts:2:0:1"],
        "covered": ["src/index.ts:2:0:0"],
        "unit": "branch",
        "files_measured": ["src/index.ts"],
    }
    assert report["files_measured"] == ["src/index.ts"]
    assert "per_test" not in report


def test_lcov_sidecar_normalizes_file_contexts_and_preserves_empty_tests(
    tmp_path: Path, source: Path
) -> None:
    sidecar = tmp_path / "lcov.info.per-test.json"
    sidecar.write_text(
        json.dumps(
            {
                "per_test_numbits": {
                    "/app/src/index.ts": {
                        "suite.test.ts::suite > one": lines_to_numbits({1, 2}),
                        "suite.test.ts::suite > no source calls": lines_to_numbits(set()),
                        "": lines_to_numbits({1}),
                    },
                }
            }
        )
    )
    report = read_report(tmp_path, source, REPORT)
    assert report["per_test_numbits"] == {
        "suite.test.ts::suite > one": {"src/index.ts": lines_to_numbits({1, 2})},
        "suite.test.ts::suite > no source calls": {"src/index.ts": ""},
    }
    sidecar.write_text('{"per_test_numbits": {}}')
    assert "per_test_numbits" not in read_report(tmp_path, source, REPORT)
    sidecar.unlink()
    assert "per_test_numbits" not in read_report(tmp_path, source, REPORT)


@pytest.mark.parametrize(
    "content",
    [
        '{"per_test_numbits": {}, "per_test_numbits": {}}',
        "{}",
        '{"per_test_numbits": []}',
        '{"per_test_numbits": {"src/index.ts": {"a": "Ag==", "a": "Ag=="}}}',
        json.dumps({"per_test_numbits": {"src/missing.ts": {"a": "Ag=="}}}),
        json.dumps({"per_test_numbits": {"../src/index.ts": {"a": "Ag=="}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": 3}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": "!"}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": lines_to_numbits({0})}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": lines_to_numbits({2})}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": lines_to_numbits({3})}}}),
        json.dumps({"per_test_numbits": {"src/index.ts": {"a": lines_to_numbits({4})}}}),
        json.dumps({"per_test_numbits": {"src/other.ts": {"a": "Ag=="}}}),
        json.dumps(
            {
                "per_test_numbits": {
                    "src/index.ts": {"a": "Ag=="},
                    "/app/src/index.ts": {"b": "Ag=="},
                }
            }
        ),
    ],
)
def test_lcov_sidecar_rejects_invalid_or_uncovered_coordinates(
    tmp_path: Path, source: Path, content: str
) -> None:
    (source / "src/other.ts").write_text("export const other = 1;\n")
    (tmp_path / "lcov.info.per-test.json").write_text(content)
    with pytest.raises(ValueError):
        read_report(tmp_path, source, "SF:src/index.ts\nDA:1,1\nDA:2,0\nend_of_record\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="inspector spike requires Node")
@pytest.mark.parametrize("with_per_test", [True, False])
def test_inspector_fixture_feeds_existing_redundancy_measure(
    tmp_path: Path, with_per_test: bool
) -> None:
    from pluginbench import assessment_metrics

    fixture = Path(__file__).parent / "fixtures/js-per-test-coverage"
    result = subprocess.run(
        ["node", "--test", str(fixture / "suite.test.mjs")],
        env={**os.environ, "PLUGINBENCH_SPIKE_OUTPUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    raw = json.loads((tmp_path / "lcov.info.per-test.json").read_text())
    expected = {
        "suite.test.mjs::classification > positive": {2, 3},
        "suite.test.mjs::classification > redundant positive": {2, 3},
        "suite.test.mjs::classification > distinct async": {2, 5, 9, 10},
    }
    assert {
        name: numbits_to_lines(bits)
        for name, bits in raw["per_test_numbits"]["subject.mjs"].items()
    } == expected

    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "subject.mjs").write_text("export const old = 0;\n")
    shutil.copyfile(fixture / "subject.mjs", after / "subject.mjs")
    change = assessment_metrics.compare_snapshots(before, after)
    if not with_per_test:
        (tmp_path / "lcov.info.per-test.json").unlink()
    parsed = parse_coverage(
        "lcov",
        tmp_path / "lcov.info",
        snapshot=after,
        source_scope=["*.mjs"],
        test_scope=["*.test.mjs"],
        workdir="/app",
    )
    metrics = assessment_metrics._coverage(parsed, {"subject.mjs"}, ["*.mjs"])
    provenance = {
        "tool": "inspector",
        "tool_version": "spike",
        "config_digest": "same",
        "scope": ["*.mjs"],
    }
    (tmp_path / "before.info").write_text("SF:subject.mjs\nDA:1,1\nend_of_record\n")
    (tmp_path / "before.info.per-test.json").write_text(
        json.dumps(
            {
                "per_test_numbits": {
                    "subject.mjs": {
                        "suite.test.mjs::classification > positive": lines_to_numbits({1})
                    },
                }
            }
        )
    )
    starting = assessment_metrics._coverage(
        parse_coverage(
            "lcov",
            tmp_path / "before.info",
            snapshot=before,
            source_scope=["*.mjs"],
            test_scope=["*.test.mjs"],
            workdir="/app",
        ),
        {"subject.mjs"},
        ["*.mjs"],
    )
    compared = assessment_metrics.compare_evidence(
        {
            "coverage": {"provenance": provenance, "metrics": starting},
            "snapshot_digest": change["before_digest"],
        },
        {
            "coverage": {"provenance": provenance, "metrics": metrics},
            "snapshot_digest": change["after_digest"],
        },
        change=change,
    )
    coverage = compared["coverage"]
    assert isinstance(coverage, dict)
    changed = coverage["changed_lines"]
    if with_per_test:
        assert changed["added_tests"] == 2
        assert changed["redundant_added_test_names"] == [
            "suite.test.mjs::classification > redundant positive",
        ]
        assert changed["redundant_added_tests"] == 1
    else:
        assert "per_test_numbits" not in parsed
        assert changed["redundant_added_tests"] is None
        assert changed["redundancy_reason"] == (
            "per-test line coverage was not recorded for both snapshots"
        )


@pytest.mark.parametrize("path", ["/private/index.ts", "../src/index.ts", "src/missing.ts"])
def test_source_escape_and_invented_file_rejected(tmp_path: Path, source: Path, path: str) -> None:
    with pytest.raises(ValueError, match="escapes|absent"):
        read_report(tmp_path, source, REPORT.replace("/app/src/index.ts", path))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("FNF:1", "FNF:2"),
        ("FNDA:2,value", "FNDA:nan,value"),
        ("BRDA:2,0,1,-", "BRDA:2,0,0,1"),
        ("FN:1,value", "FN:1,value\nFN:1,value"),
        ("FNDA:2,value", "FNDA:2,unknown"),
        ("FNF:1", "FNF:1\nFNF:1"),
        ("LF:2", "LF:3"),
        ("end_of_record\n", ""),
    ],
)
def test_invalid_populations_rejected(tmp_path: Path, source: Path, old: str, new: str) -> None:
    with pytest.raises(ValueError):
        read_report(tmp_path, source, REPORT.replace(old, new))


def test_line_only_coverage_keeps_missing_kinds_unknown(tmp_path: Path, source: Path) -> None:
    report = read_report(tmp_path, source, "SF:src/index.ts\nDA:1,1\nend_of_record\n")
    assert report["lines"] == {
        "total": ["src/index.ts:1"],
        "covered": ["src/index.ts:1"],
        "unit": "line",
        "files_measured": ["src/index.ts"],
    }
    assert "functions" not in report and "branches" not in report


def test_explicit_empty_population_is_distinct_from_missing(tmp_path: Path, source: Path) -> None:
    report = read_report(
        tmp_path, source, "SF:src/index.ts\nFNF:0\nFNH:0\nBRF:0\nBRH:0\nend_of_record\n"
    )
    for kind in ("functions", "branches"):
        population = report[kind]
        assert isinstance(population, dict)
        assert population["total"] == []
        assert population["covered"] == []
    assert "lines" not in report


def test_duplicate_source_records_are_rejected(tmp_path: Path, source: Path) -> None:
    with pytest.raises(ValueError, match="duplicated"):
        read_report(tmp_path, source, REPORT + REPORT)


def test_partial_lcov_population_keeps_measured_files(tmp_path: Path, source: Path) -> None:
    (source / "src/other.ts").write_text("export const other = 1;\n")
    report = read_report(tmp_path, source, REPORT + "SF:src/other.ts\nDA:1,0\nend_of_record\n")
    assert isinstance(report["functions"], dict)
    assert isinstance(report["lines"], dict)
    assert report["functions"]["files_measured"] == ["src/index.ts"]
    assert report["lines"]["files_measured"] == ["src/index.ts", "src/other.ts"]


# What Jest's istanbul instrumenter mapped to each file: index.ts has code, options.ts only types.
INSTRUMENTED = "5 src/index.ts\n0 src/options.ts\n"


def instrumented_metrics(
    tmp_path: Path, source: Path, counts: str | None, report: str = REPORT
) -> dict[str, object]:
    """Parse an LCOV report beside its instrumented counts, then score it as evidence does."""
    (source / "src/options.ts").write_text("export interface Options {\n  width: number;\n}\n")
    if counts is not None:
        (tmp_path / "lcov.info.instrumented.txt").write_text(counts)
    parsed = read_report(tmp_path, source, report)
    snapshot = inspect_snapshot(source)["files"]
    assert isinstance(snapshot, dict)
    return assessment_metrics._coverage(parsed, set(snapshot), ["src/**"])


def test_a_types_only_file_jest_leaves_out_has_nothing_to_measure(
    tmp_path: Path, source: Path
) -> None:
    metrics = instrumented_metrics(tmp_path, source, INSTRUMENTED)
    assert metrics["population_complete_for_declared_scope"] is True
    assert metrics["line_population_complete"] is True
    assert metrics["unmeasured_source_files"] == []
    assert metrics["lcov_files_without_statements"] == ["src/options.ts"]


@pytest.mark.parametrize(
    "counts",
    [
        None,  # The command wrote no counts at all.
        INSTRUMENTED.replace("0 src/options.ts\n", ""),  # It could not count options.ts.
        INSTRUMENTED.replace("0 src/options.ts", "3 src/options.ts"),
    ],
    ids=["no-record", "no-line", "statements"],
)
def test_omitted_lcov_files_without_a_zero_count_stay_expected(
    tmp_path: Path, source: Path, counts: str | None
) -> None:
    metrics = instrumented_metrics(tmp_path, source, counts)
    assert metrics["population_complete_for_declared_scope"] is False
    assert metrics["unmeasured_source_files"] == ["src/options.ts"]
    if counts is None:
        assert "lcov_files_without_statements" not in metrics
    else:
        assert metrics["lcov_files_without_statements"] == []


@pytest.mark.parametrize(
    ("counts", "error"),
    [
        (INSTRUMENTED.replace("5 src/index.ts", "0 src/index.ts"), "0 for measured files"),
        (INSTRUMENTED + "0 src/ghost.ts\n", "absent from the frozen snapshot"),
        (INSTRUMENTED + "0 src/options.ts\n", "more than once"),
        (INSTRUMENTED + "none src/other.ts\n", "malformed"),
    ],
    ids=["measured", "absent", "duplicate", "malformed"],
)
def test_instrumented_counts_that_disagree_with_the_snapshot_or_report_are_rejected(
    tmp_path: Path, source: Path, counts: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        instrumented_metrics(tmp_path, source, counts)


@pytest.mark.parametrize("counts", [(2, 3), (0, 0), (2, 0)])
def test_repeated_callback_names_preserve_provable_coverage(
    tmp_path: Path, source: Path, counts: tuple[int, int]
) -> None:
    (source / "src/callbacks.ts").write_text(
        "export const callbacks = [\nfunction op() { return 1; },\n"
        "function op() { return 2; }\n];\n"
    )
    content = (
        "SF:src/callbacks.ts\nFN:2,op\nFN:3,op\n"
        f"FNDA:{counts[0]},op\nFNDA:{counts[1]},op\n"
        f"FNF:2\nFNH:{sum(count > 0 for count in counts)}\nDA:1,1\n"
        "LF:1\nLH:1\nend_of_record\n"
    )
    report = read_report(tmp_path, source, content)
    assert isinstance(report["lines"], dict)
    assert report["lines"]["covered"] == ["src/callbacks.ts:1"]
    if bool(counts[0]) == bool(counts[1]):
        assert isinstance(report["functions"], dict)
        assert report["functions"]["total"] == ["src/callbacks.ts:2:op", "src/callbacks.ts:3:op"]
        assert report["functions"]["covered"] == (report["functions"]["total"] if counts[0] else [])
    else:
        assert "functions" not in report
        assert isinstance(report["unavailable_populations"], dict)
        assert report["unavailable_populations"]["functions"]["files"] == ["src/callbacks.ts"]
        partial = read_report(tmp_path, source, REPORT + content)
        assert isinstance(partial["functions"], dict)
        assert partial["functions"]["files_measured"] == ["src/index.ts"]


def python_report(tmp_path: Path, source: Path, data: PythonCoverageData) -> dict[str, object]:
    (source / "src/value.py").write_text(
        "def value(x):\n    if x:\n        return 1\n    return 0\n"
    )
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(data))
    return parse_coverage(
        "coverage-py-json",
        report,
        snapshot=source,
        source_scope=["src/**"],
        test_scope=["test/**"],
        workdir="/app",
    )


def python_data(branches: bool = True) -> PythonCoverageData:
    summary = {"num_statements": 4, "covered_lines": 3, "missing_lines": 1, "excluded_lines": 0}
    data: PythonCoverageData = {
        "meta": {"format": 3, "version": "7.16.2", "branch_coverage": branches},
        "files": {
            "src/value.py": {
                "executed_lines": [1, 2, 3],
                "missing_lines": [4],
                "excluded_lines": [],
                "summary": summary,
            }
        },
    }
    if branches:
        summary.update(num_branches=2, covered_branches=1, missing_branches=1)
        data["files"]["src/value.py"].update(
            {"executed_branches": [[2, 3]], "missing_branches": [[2, 4]]}
        )
    return data


def test_python_coordinates_and_branch_mode(tmp_path: Path, source: Path) -> None:
    report = python_report(tmp_path, source, python_data())
    assert isinstance(report["lines"], dict)
    assert isinstance(report["branches"], dict)
    assert report["lines"]["total"] == [f"src/value.py:{line}" for line in range(1, 5)]
    assert report["branches"]["covered"] == ["src/value.py:2:3"]
    assert "functions" not in report and "statements" not in report
    report = python_report(tmp_path, source, python_data(False))
    assert "branches" not in report


def test_python_per_test_numbits_are_validated_and_kept_compact(
    tmp_path: Path, source: Path
) -> None:
    data = python_data()
    # coverage.py numbits: one bit per line, as written by the enrichment step from contexts.
    data["per_test_numbits"] = {
        "src/value.py": {
            "": lines_to_numbits({1}),
            "test_a": lines_to_numbits({2, 3}),
            "test_b": lines_to_numbits({2}),
        }
    }
    report = python_report(tmp_path, source, data)
    # The empty context is code run outside any test, such as imports.
    per_test = report["per_test_numbits"]
    assert isinstance(per_test, dict) and set(per_test) == {"test_a", "test_b"}
    assert numbits_to_lines(per_test["test_a"]["src/value.py"]) == {2, 3}
    assert "per_test_numbits" not in python_report(tmp_path, source, python_data())
    # Contexts without test names (dynamic contexts off) mean per-test data is unknown, not empty.
    data["per_test_numbits"] = {"src/value.py": {"": lines_to_numbits({1, 2})}}
    assert "per_test_numbits" not in python_report(tmp_path, source, data)
    # Line 4 is a statement the report calls missing; line 9 is outside the file.
    for bad in ({4}, {9}):
        data["per_test_numbits"] = {"src/value.py": {"test_a": lines_to_numbits(bad)}}
        with pytest.raises(ValueError, match="unexecuted"):
            python_report(tmp_path, source, data)


def test_python_module_entry_line_of_an_empty_module_is_accepted(
    tmp_path: Path, source: Path
) -> None:
    # coverage.py reports line 0, the module entry, as executed for a file without statements.
    (source / "src/__init__.py").write_text("")
    data = python_data()
    data["files"]["src/__init__.py"] = {
        "executed_lines": [0],
        "missing_lines": [],
        "excluded_lines": [],
        "summary": {
            "num_statements": 0, "covered_lines": 0, "missing_lines": 0, "excluded_lines": 0,
            "num_branches": 0, "covered_branches": 0, "missing_branches": 0,
        },
        "executed_branches": [],
        "missing_branches": [],
    }
    data["per_test_numbits"] = {"src/__init__.py": {"test_a": lines_to_numbits({0})}}
    report = python_report(tmp_path, source, data)
    assert isinstance(report["lines"], dict)
    assert report["lines"]["total"] == [f"src/value.py:{line}" for line in range(1, 5)]
    assert "per_test_numbits" not in report


def test_python_statement_coordinates_exclude_traced_nonstatements(
    tmp_path: Path, source: Path
) -> None:
    data = python_data()
    data["meta"]["statement_lines_source"] = "coverage.Coverage.analysis2"
    file = data["files"]["src/value.py"]
    file.update({"statement_lines": [1, 2, 3], "executed_lines": [1, 2, 3, 4], "missing_lines": []})
    file["summary"].update(num_statements=3, covered_lines=3, missing_lines=0)
    report = python_report(tmp_path, source, data)
    assert isinstance(report["lines"], dict)
    assert (
        report["lines"]["total"]
        == report["lines"]["covered"]
        == ["src/value.py:1", "src/value.py:2", "src/value.py:3"]
    )


def coverage_7_2_data(meta: dict[str, object]) -> PythonCoverageData:
    """python_data without branches, under the given meta: coverage.py 7.2.x writes no format."""
    data = python_data(False)
    data["meta"] = meta
    return data


def test_python_report_without_format_from_coverage_7_2_reads_as_format_2(
    tmp_path: Path, source: Path
) -> None:
    meta: dict[str, object] = {"version": "7.2.7", "branch_coverage": False}
    report = python_report(tmp_path, source, coverage_7_2_data(meta))
    assert report == python_report(tmp_path, source, coverage_7_2_data({**meta, "format": 2}))


@pytest.mark.parametrize(
    "meta",
    [
        # Other releases without meta.format stay refused, though 7.3.0 to 7.4.0 write the same shape.
        {"version": version, "branch_coverage": False}
        for version in ("7.4.0", "7.2", "7.2.7a1", "7.2.\u0663")
    ]
    + [{"version": "7.2.7", "branch_coverage": False, "format": None}],
)
def test_python_report_without_a_known_format_is_rejected(
    tmp_path: Path, source: Path, meta: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="nonnegative integer: None"):
        python_report(tmp_path, source, coverage_7_2_data(meta))


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "outside",
        "overlap",
        "summary",
        "branch",
        "escape",
        "statement_partition",
        "statement_source",
        "statement_source_unknown",
        "statement_file_absent",
        "statement_duplicate",
        "statement_outside",
    ],
)
def test_python_malformed_populations_rejected(tmp_path: Path, source: Path, fault: str) -> None:
    data = python_data()
    file = data["files"]["src/value.py"]
    if fault == "duplicate":
        file["executed_lines"].append(1)
    elif fault == "outside":
        file["missing_lines"] = [5]
    elif fault == "overlap":
        file["missing_lines"] = [3]
    elif fault == "summary":
        file["summary"]["num_statements"] = 8
    elif fault == "branch":
        file["missing_branches"] = [[2, -8]]
    elif fault == "escape":
        data["files"]["../value.py"] = data["files"].pop("src/value.py")
    else:
        data["meta"]["statement_lines_source"] = "coverage.Coverage.analysis2"
        file["statement_lines"] = [1, 2, 3, 4]
        if fault == "statement_partition":
            file["executed_lines"] = [1, 2]
            file["summary"].update(num_statements=3, covered_lines=2)
        elif fault == "statement_source":
            del data["meta"]["statement_lines_source"]
        elif fault == "statement_source_unknown":
            data["meta"]["statement_lines_source"] = []
        elif fault == "statement_file_absent":
            del file["statement_lines"]
        elif fault == "statement_duplicate":
            file["statement_lines"].append(1)
        else:
            file["statement_lines"].append(5)
    with pytest.raises(ValueError):
        python_report(tmp_path, source, data)


def test_python_excluded_lines_and_exit_arc(tmp_path: Path, source: Path) -> None:
    data = python_data()
    file = data["files"]["src/value.py"]
    file["missing_lines"] = []
    file["excluded_lines"] = [4]
    file["summary"].update(num_statements=3, missing_lines=0, excluded_lines=1)
    file["missing_branches"] = [[2, -1]]
    report = python_report(tmp_path, source, data)
    assert isinstance(report["lines"], dict)
    assert isinstance(report["branches"], dict)
    assert "src/value.py:4" not in report["lines"]["total"]
    assert "src/value.py:2:-1" in report["branches"]["total"]


GO_REPORT = """mode: set
example.test/value/value.go:2.23,3.9 2 1
example.test/value/value.go:3.9,3.21 1 0
"""


def go_report(tmp_path: Path, source: Path, content: str) -> dict[str, object]:
    (source / "go.mod").write_text("module example.test/value\n\ngo 1.24\n")
    (source / "value.go").write_text(
        "package value\nfunc Value(x int) int {\n    if x > 0 { return 1 }\n    return 0\n}\n"
    )
    report = tmp_path / "cover.out"
    report.write_text(content)
    return parse_coverage(
        "go-coverprofile",
        report,
        snapshot=source,
        source_scope=["*.go"],
        test_scope=["*_test.go"],
        workdir="/app",
    )


def test_go_statement_weights_and_frozen_module_mapping(tmp_path: Path, source: Path) -> None:
    report = go_report(tmp_path, source, GO_REPORT)
    assert report["statements"] == {
        "total": ["value.go:2.23,3.9", "value.go:3.9,3.21"],
        "covered": ["value.go:2.23,3.9"],
        "weights": {"value.go:2.23,3.9": 2, "value.go:3.9,3.21": 1},
        "unit": "statement",
        "files_measured": ["value.go"],
    }
    assert not {"lines", "branches", "functions"} & report.keys()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("mode: set", "mode: strange"),
        ("2 1", "2 2"),
        ("2.23,3.9", "2.99,3.9"),
        ("2.23,3.9", "3.9,2.23"),
        ("2 1", "-1 1"),
        ("example.test/value", "external.invalid/value"),
        ("value.go:2.23,3.9", "../value.go:2.23,3.9"),
    ],
)
def test_go_malformed_populations_rejected(
    tmp_path: Path, source: Path, old: str, new: str
) -> None:
    with pytest.raises(ValueError):
        go_report(tmp_path, source, GO_REPORT.replace(old, new))


def test_go_empty_case_block_has_no_width_and_no_statements(tmp_path: Path, source: Path) -> None:
    # go1.25 writes `file:226.18,226.18 0 1` for an empty `case` clause.
    report = go_report(tmp_path, source, GO_REPORT + "example.test/value/value.go:4.5,4.5 0 1\n")
    statements = report["statements"]
    assert isinstance(statements, dict)
    assert statements["weights"]["value.go:4.5,4.5"] == 0
    with pytest.raises(ValueError, match="escapes source span"):
        go_report(tmp_path, source, GO_REPORT + "example.test/value/value.go:4.5,4.5 1 1\n")


def test_go_duplicate_blocks_are_rejected(tmp_path: Path, source: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        go_report(tmp_path, source, GO_REPORT + GO_REPORT.splitlines()[1] + "\n")


def test_go_nested_module_uses_specific_frozen_identity(tmp_path: Path, source: Path) -> None:
    go_report(tmp_path, source, GO_REPORT)
    nested = source / "nested"
    nested.mkdir()
    (nested / "go.mod").write_text("module example.test/value/nested\n")
    (nested / "value.go").write_text((source / "value.go").read_text())
    report = tmp_path / "cover.out"
    report.write_text(GO_REPORT.replace("example.test/value/", "example.test/value/nested/"))
    result = parse_coverage(
        "go-coverprofile",
        report,
        snapshot=source,
        source_scope=["nested/*.go"],
        test_scope=["nested/*_test.go"],
        workdir="/app",
    )
    assert result["files_measured"] == ["nested/value.go"]


GO_LIST = """{
\t"Dir": "/app",
\t"ImportPath": "example.test/value",
\t"GoFiles": ["added.go", "doc.go", "value.go"],
\t"IgnoredGoFiles": ["value_windows.go"]
}
"""
# What `go tool cover` declared per non-test file: `Count [N]uint32`, N blocks.
GO_BLOCKS = "1 added.go\n0 doc.go\n1 testdata/fixture.go\n2 value.go\n1 value_windows.go\n"
ADDED_BLOCK = "example.test/value/added.go:2.20,2.30 1 1\n"


def go_build_metrics(
    tmp_path: Path,
    source: Path,
    *,
    profile: str,
    go_list: str = GO_LIST,
    blocks: str | None = GO_BLOCKS,
    extra_files: dict[str, str] | None = None,
) -> dict[str, object]:
    """Parse a Go report beside its build sidecars, then score it as evidence does."""
    files = {
        "doc.go": "// Package value has a doc file, which holds no statements.\npackage value\n",
        "added.go": "package value\nfunc Added() int { return 1 }\n",
        "value_windows.go": "//go:build windows\n\npackage value\n\nfunc Windows() {}\n",
        "testdata/fixture.go": "package fixture\n\nfunc Fixture() {}\n",
        **(extra_files or {}),
    }
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    (tmp_path / "cover.out.go-list.json").write_text(go_list)
    if blocks is not None:
        (tmp_path / "cover.out.go-blocks.txt").write_text(blocks)
    report = go_report(tmp_path, source, profile)
    snapshot = inspect_snapshot(source)["files"]
    assert isinstance(snapshot, dict)
    return assessment_metrics._coverage(report, set(snapshot), ["*.go"])


def test_go_files_the_build_does_not_compile_leave_the_report_complete(
    tmp_path: Path, source: Path
) -> None:
    metrics = go_build_metrics(tmp_path, source, profile=GO_REPORT + ADDED_BLOCK)
    assert metrics["population_complete_for_declared_scope"] is True
    assert metrics["statement_population_complete"] is True
    assert metrics["unmeasured_source_files"] == []
    assert metrics["go_files_not_compiled"] == {
        "testdata/fixture.go": "in no package that go list -deps -test listed",
        "value_windows.go": "excluded from package example.test/value by build constraints",
    }
    # Go's cover tool declared no blocks for doc.go: nothing to measure.
    assert metrics["go_files_without_statements"] == ["doc.go"]


def test_a_go_file_with_only_func_types_has_nothing_to_measure(
    tmp_path: Path, source: Path
) -> None:
    # expr's internal/testify/require/requirements.go: compiled, only `type X func(...)`.
    metrics = go_build_metrics(
        tmp_path,
        source,
        profile=GO_REPORT + ADDED_BLOCK,
        go_list=GO_LIST.replace('"doc.go",', '"doc.go", "types.go",'),
        blocks=GO_BLOCKS + "0 types.go\n",
        extra_files={"types.go": "package value\n\ntype Check func(int) bool\n"},
    )
    assert metrics["population_complete_for_declared_scope"] is True
    assert metrics["unmeasured_source_files"] == []
    assert metrics["go_files_without_statements"] == ["doc.go", "types.go"]


@pytest.mark.parametrize(
    "blocks",
    [
        None,  # The command wrote no cover record at all.
        GO_BLOCKS.replace("0 doc.go\n", ""),  # cover failed on doc.go, so no line.
        GO_BLOCKS.replace("0 doc.go", "1 doc.go"),
    ],
    ids=["no-record", "no-line", "blocks"],
)
def test_go_files_cover_did_not_declare_empty_stay_expected(
    tmp_path: Path, source: Path, blocks: str | None
) -> None:
    metrics = go_build_metrics(tmp_path, source, profile=GO_REPORT + ADDED_BLOCK, blocks=blocks)
    assert metrics["population_complete_for_declared_scope"] is False
    assert metrics["unmeasured_source_files"] == ["doc.go"]
    assert metrics["go_files_without_statements"] == []


def test_a_compiled_go_file_missing_from_the_report_stays_incomplete(
    tmp_path: Path, source: Path
) -> None:
    # added.go stands for a file an agent adds to a compiled package and leaves untested.
    metrics = go_build_metrics(tmp_path, source, profile=GO_REPORT)
    assert metrics["population_complete_for_declared_scope"] is False
    assert metrics["statement_population_complete"] is False
    assert metrics["unmeasured_source_files"] == ["added.go"]


@pytest.mark.parametrize(
    ("directory", "helper"),
    [
        # value_test.go imports a helper that does not import the package: go lists it plain.
        (
            "testdata/helper",
            '{"Dir": "/app/testdata/helper", "ImportPath": "example.test/value/testdata/helper",'
            ' "GoFiles": ["helper.go"]}\n',
        ),
        # x_test.go imports a helper that imports the package: go lists only its test variant.
        (
            "_tools/helper",
            '{"Dir": "/app/_tools/helper", "ImportPath":'
            ' "example.test/value/_tools/helper [example.test/value.test]",'
            ' "ForTest": "example.test/value", "GoFiles": ["helper.go"]}\n',
        ),
    ],
    ids=["internal-test-import", "external-test-import"],
)
def test_go_files_only_tests_import_stay_expected(
    tmp_path: Path, source: Path, directory: str, helper: str
) -> None:
    # The shape go1.25 `go list -e -deps -test -json` writes: the standard library, packages
    # that failed to load, test variants that repeat the package's files with its `_test.go`
    # files, and the synthesized `.test` main, whose only file is in the build cache.
    go_list = (
        GO_LIST
        + '{"Dir": "/usr/local/go/src/testing", "ImportPath": "testing",'
        ' "GoFiles": ["testing.go"]}\n'
        '{"ImportPath": "missing.example/x"}\n'
        + helper
        + '{"Dir": "/app", "ImportPath": "example.test/value [example.test/value.test]",'
        ' "ForTest": "example.test/value",'
        ' "GoFiles": ["added.go", "doc.go", "value.go", "value_test.go"],'
        ' "IgnoredGoFiles": ["value_windows.go"]}\n'
        '{"Dir": "/app", "ImportPath": "example.test/value_test [example.test/value.test]",'
        ' "ForTest": "example.test/value", "GoFiles": ["x_test.go"]}\n'
        '{"Dir": "/app", "ImportPath": "example.test/value.test",'
        ' "GoFiles": ["/root/.cache/go-build/16/16b6c34b-d"]}\n'
    )
    metrics = go_build_metrics(
        tmp_path,
        source,
        profile=GO_REPORT + ADDED_BLOCK,
        go_list=go_list,
        blocks=GO_BLOCKS + f"1 {directory}/helper.go\n",
        extra_files={
            f"{directory}/helper.go": "package helper\n\nfunc Help() {}\n",
            "value_test.go": "package value\n",
            "x_test.go": "package value_test\n",
        },
    )
    assert metrics["population_complete_for_declared_scope"] is False
    assert metrics["unmeasured_source_files"] == [f"{directory}/helper.go"]
    assert metrics["go_files_not_compiled"] == {
        "testdata/fixture.go": "in no package that go list -deps -test listed",
        "value_windows.go": "excluded from package example.test/value by build constraints",
    }


def test_vendored_go_packages_are_out_of_scope(tmp_path: Path, source: Path) -> None:
    # `-deps` lists vendored packages from inside the workdir; they are not the repo's code.
    go_list = GO_LIST + (
        '{"Dir": "/app/vendor/example.com/dep", "ImportPath": "example.com/dep",'
        ' "GoFiles": ["dep.go"], "IgnoredGoFiles": ["dep_windows.go"]}\n'
    )
    metrics = go_build_metrics(
        tmp_path,
        source,
        profile=GO_REPORT + ADDED_BLOCK,
        go_list=go_list,
        extra_files={
            "vendor/example.com/dep/dep.go": "package dep\n\nfunc D() int { return 2 }\n",
            "vendor/example.com/dep/dep_windows.go": "//go:build windows\n\npackage dep\n",
        },
    )
    assert metrics["population_complete_for_declared_scope"] is True
    assert metrics["unmeasured_source_files"] == []
    assert metrics["go_files_not_compiled"] == {
        "testdata/fixture.go": "in no package that go list -deps -test listed",
        "value_windows.go": "excluded from package example.test/value by build constraints",
        "vendor/example.com/dep/dep.go": "vendored dependency",
        "vendor/example.com/dep/dep_windows.go": "vendored dependency",
    }


# anko's goyacc output: Go names each function's blocks after the `//line` target its position
# falls in, with that target's line numbers and no columns, so the two targets' lines collide.
LINE_DIRECTED_PARSER = """package value
func Own() int { return 1 }

//line parser.go.y:2
import "strconv"

//line yaccpar:1
func Parse(text string) int {
\tvalue, err := strconv.Atoi(text)
\tif err != nil {
//line parser.go.y:3
\t\treturn 0
\t}
\treturn value
}
"""
LINE_DIRECTED_BLOCKS = (
    "example.test/value/parser.go:2.16,2.28 1 1\n"
    "example.test/value/yaccpar:2.0,4.0 2 1\n"
    "example.test/value/yaccpar:3.0,4.0 1 0\n"
    "example.test/value/yaccpar:3.0,4.0 1 1\n"
    "example.test/value/parser.go.y:5.0,6.0 1 0\n"
)
LINE_DIRECTED_FILES = {"parser.go": LINE_DIRECTED_PARSER, "parser.go.y": "%%\n"}


def test_go_blocks_named_after_a_line_directive_target_count_for_the_file_naming_it(
    tmp_path: Path, source: Path
) -> None:
    for name, text in LINE_DIRECTED_FILES.items():
        (source / name).write_text(text)
    report = go_report(tmp_path, source, GO_REPORT + LINE_DIRECTED_BLOCKS)
    assert report["files_measured"] == ["parser.go", "value.go"]
    statements = report["statements"]
    assert isinstance(statements, dict)
    # Keyed by target and Go's coordinates, repeats numbered in profile order, beside the
    # file's own block, each with Go's weight.
    assert {
        key: weight for key, weight in statements["weights"].items() if key.startswith("parser.go")
    } == {
        "parser.go:2.16,2.28": 1,
        "parser.go:yaccpar:2.0,4.0": 2,
        "parser.go:yaccpar:3.0,4.0": 1,
        "parser.go:yaccpar:3.0,4.0#2": 1,
        "parser.go:parser.go.y:5.0,6.0": 1,
    }
    assert [key for key in statements["covered"] if key.startswith("parser.go")] == [
        "parser.go:2.16,2.28",
        "parser.go:yaccpar:2.0,4.0",
        "parser.go:yaccpar:3.0,4.0#2",
    ]


def test_a_go_file_whose_blocks_go_names_after_a_line_directive_target_is_measured(
    tmp_path: Path, source: Path
) -> None:
    metrics = go_build_metrics(
        tmp_path,
        source,
        profile=GO_REPORT + ADDED_BLOCK + LINE_DIRECTED_BLOCKS,
        go_list=GO_LIST.replace('"added.go",', '"added.go", "parser.go",'),
        blocks=GO_BLOCKS + "5 parser.go\n",
        extra_files=LINE_DIRECTED_FILES,
    )
    assert metrics["population_complete_for_declared_scope"] is True
    assert metrics["unmeasured_source_files"] == []
    # value.go 3, added.go 1, parser.go 6 statements, of which 2 + 1 + 4 are covered.
    assert (metrics["statement_total"], metrics["statement_covered"]) == (10, 7)


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({}, "absent from the frozen snapshot: .*yaccpar"),
        # cover never instruments test files, and another package's directive is not this one's
        ({"parser_test.go": LINE_DIRECTED_PARSER}, "absent from the frozen snapshot: .*yaccpar"),
        ({"other/parser.go": LINE_DIRECTED_PARSER}, "absent from the frozen snapshot: .*yaccpar"),
        # A second file naming the target, if only in a comment, leaves the blocks no one owner.
        (
            {"parser.go": LINE_DIRECTED_PARSER, "notes.go": "package value\n// /*line yaccpar:1*/\n"},
            "named by more than one file",
        ),
    ],
    ids=["no-directive", "test-file", "other-package", "two-files"],
)
def test_go_blocks_no_single_line_directive_explains_are_refused(
    tmp_path: Path, source: Path, files: dict[str, str], error: str
) -> None:
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    with pytest.raises(ValueError, match=error):
        go_report(tmp_path, source, GO_REPORT + LINE_DIRECTED_BLOCKS.split("\n", 1)[1])


def test_a_line_directive_naming_a_frozen_go_file_keeps_its_blocks_there(
    tmp_path: Path, source: Path
) -> None:
    # As cgo's generated files do: the blocks belong to the file the directive names.
    (source / "generated.go").write_text("package value\n//line value.go:2\nfunc G() {}\n")
    report = go_report(tmp_path, source, GO_REPORT)
    assert report["files_measured"] == ["value.go"]


@pytest.mark.parametrize(
    ("go_list", "blocks", "error"),
    [
        (
            GO_LIST.replace('"value_windows.go"', '"value_windows.go", "parser.go"'),
            GO_BLOCKS,
            "did not compile",
        ),
        (
            GO_LIST.replace('"added.go",', '"added.go", "parser.go",'),
            GO_BLOCKS + "0 parser.go\n",
            "declared no blocks for measured files: \\['parser.go'\\]",
        ),
    ],
    ids=["not-compiled", "no-blocks"],
)
def test_go_line_directive_blocks_the_build_disowns_are_rejected(
    tmp_path: Path, source: Path, go_list: str, blocks: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        go_build_metrics(
            tmp_path,
            source,
            profile=GO_REPORT + ADDED_BLOCK + LINE_DIRECTED_BLOCKS,
            go_list=go_list,
            blocks=blocks,
            extra_files=LINE_DIRECTED_FILES,
        )


def test_a_repeated_section_of_line_directive_blocks_is_refused(
    tmp_path: Path, source: Path
) -> None:
    # Target rows skip the duplicate check, so the declared count bounds them instead. The
    # file's own row counts too: 1 own row and 4 + 1 target rows exceed the 5 declared.
    repeated = LINE_DIRECTED_BLOCKS + "example.test/value/yaccpar:2.0,4.0 2 1\n"
    with pytest.raises(ValueError, match="more blocks than go cover declared for: \\['parser.go"):
        go_build_metrics(
            tmp_path,
            source,
            profile=GO_REPORT + ADDED_BLOCK + repeated,
            go_list=GO_LIST.replace('"added.go",', '"added.go", "parser.go",'),
            blocks=GO_BLOCKS + "5 parser.go\n",
            extra_files=LINE_DIRECTED_FILES,
        )


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ('"/app"', '"/elsewhere"', "did not compile"),
        ('"doc.go",', '"doc.go", "ghost.go",', "absent from the frozen snapshot"),
        ('"added.go", ', "", "did not compile"),
    ],
)
def test_go_build_lists_that_disagree_with_the_snapshot_or_report_are_rejected(
    tmp_path: Path, source: Path, old: str, new: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        go_build_metrics(
            tmp_path, source, profile=GO_REPORT + ADDED_BLOCK, go_list=GO_LIST.replace(old, new)
        )


@pytest.mark.parametrize(
    ("blocks", "error"),
    [
        (GO_BLOCKS + "zero doc.go\n", "malformed"),
        (GO_BLOCKS + "0 doc.go\n", "more than once"),
        (GO_BLOCKS + "0 ghost.go\n", "absent from the frozen snapshot"),
        (GO_BLOCKS.replace("2 value.go", "0 value.go"), "declared no blocks"),
    ],
    ids=["malformed", "duplicate", "absent", "measured"],
)
def test_go_cover_blocks_that_disagree_with_the_snapshot_or_report_are_rejected(
    tmp_path: Path, source: Path, blocks: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        go_build_metrics(tmp_path, source, profile=GO_REPORT + ADDED_BLOCK, blocks=blocks)


GO_TEST_MAIN = """{
\t"Dir": "/app",
\t"ImportPath": "example.test/value.test",
\t"GoFiles": ["/root/.cache/go-build/ab/abcdef-d"]
}
"""


def test_a_go_list_without_test_mains_is_refused_when_the_repo_has_tests(
    tmp_path: Path, source: Path
) -> None:
    tests = {"value_test.go": "package value\n"}
    with pytest.raises(ValueError, match="-deps -test"):
        go_build_metrics(
            tmp_path, source, profile=GO_REPORT + ADDED_BLOCK, extra_files=tests
        )
    metrics = go_build_metrics(
        tmp_path, source, profile=GO_REPORT + ADDED_BLOCK, extra_files=tests,
        go_list=GO_LIST + GO_TEST_MAIN,
    )
    assert metrics["population_complete_for_declared_scope"] is True


@pytest.mark.parametrize("name", ["we\rird.go", "form\x0cfeed.go"])
def test_go_cover_blocks_keep_line_breaking_characters_in_file_names(
    tmp_path: Path, source: Path, name: str
) -> None:
    metrics = go_build_metrics(
        tmp_path, source, profile=GO_REPORT + ADDED_BLOCK,
        blocks=GO_BLOCKS + f"1 {name}\n", extra_files={name: "package value\n"},
    )
    assert metrics["population_complete_for_declared_scope"] is True


def test_python_duplicate_json_keys_are_rejected(tmp_path: Path, source: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text('{"files": {}, "files": {}}')
    with pytest.raises(ValueError, match="Duplicate"):
        parse_coverage(
            "coverage-py-json",
            report,
            snapshot=source,
            source_scope=["src/**"],
            test_scope=["test/**"],
            workdir="/app",
        )
