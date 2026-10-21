import json
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest

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
