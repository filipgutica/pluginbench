from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from pluginbench.assessment import _stage
from pluginbench.assessment_metrics import (
    EvidenceBinding,
    _coverage,
    _test_contribution,
    bind_evidence,
    inspect_snapshot,
)
from pluginbench.assessment_models import Attempt, Snapshot, Stage
from pluginbench.collectors import Collector, collect_measurements


def _receipt() -> dict[str, object]:
    one, two, other = "test/new.test.ts::one", "test/new.test.ts::two", "test/old.test.ts::other"
    return {
        "before_digest": "sha256:" + "a" * 64,
        "test_scope": ["test/**"],
        "contribution": {
            "method": "leave-one-out-branches-v1",
            "reference_ids": [one, two, other],
            "reference_statuses": {one: "passed", two: "passed", other: "passed"},
            "executed_ids": [one, two, other],
            "passing_ids": [one, two, other],
            "candidate_ids": [one, two],
            "selection": "all runnable cases in added/modified visible test files",
            "changed_test_files": ["test/new.test.ts"],
            "removal_runs": [
                {
                    "excluded_id": one,
                    "executed_ids": [two, other],
                    "passing_ids": [two, other],
                    "test_statuses": {two: "passed", other: "passed"},
                    "covered_branches": ["src/a.ts:1", "src/a.ts:2"],
                    "unique_branches": [],
                    "exit_code": 0,
                },
                {
                    "excluded_id": two,
                    "executed_ids": [one, other],
                    "passing_ids": [one, other],
                    "test_statuses": {one: "passed", other: "passed"},
                    "covered_branches": ["src/a.ts:1"],
                    "unique_branches": ["src/a.ts:2"],
                    "exit_code": 0,
                },
            ],
            "zero_marginal_ids": [one],
        },
    }


def _measure(report: dict[str, object]) -> dict[str, object]:
    return _test_contribution(
        report,
        {"test/new.test.ts", "test/old.test.ts"},
        {"src/a.ts:1", "src/a.ts:2", "src/a.ts:3"},
        {"src/a.ts:1", "src/a.ts:2"},
        {"src/a.ts:function:1", "src/a.ts:function:2"},
        {"src/a.ts:function:1"},
    )


def test_leave_one_out_counts_complete_candidates_without_claiming_unnecessary_tests() -> None:
    result = _measure(_receipt())
    assert result["status"] == "available"
    assert result["reference_count"] == 3
    assert result["candidate_count"] == result["tested_count"] == 2
    assert result["zero_marginal_count"] == 1
    assert "does not prove" in str(result["limitations"])


def test_no_candidate_cases_is_measured_zero_of_zero() -> None:
    report = _receipt()
    contribution = report["contribution"]
    assert isinstance(contribution, dict)
    contribution.update(
        candidate_ids=[], changed_test_files=[], removal_runs=[], zero_marginal_ids=[]
    )
    result = _measure(report)
    assert result["status"] == "available"
    assert result["candidate_count"] == result["tested_count"] == result["zero_marginal_count"] == 0


def test_equal_counts_with_different_lost_branch_sets_have_different_semantic_digests() -> None:
    first = _receipt()
    second = copy.deepcopy(first)
    row = second["contribution"]
    assert isinstance(row, dict)
    row["removal_runs"][1].update(covered_branches=["src/a.ts:2"], unique_branches=["src/a.ts:1"])
    left, right = _measure(first), _measure(second)
    assert left.pop("semantic_digest") != right.pop("semantic_digest")
    assert left == right


@pytest.mark.parametrize("kind", ["branches", "functions"])
def test_reference_covered_coordinates_are_bound_even_when_counts_match(kind: str) -> None:
    first = _receipt()
    first.update(
        files_measured=["src/a.ts"],
        branches={
            "total": ["src/a.ts:1", "src/a.ts:2", "src/a.ts:3"],
            "covered": ["src/a.ts:1", "src/a.ts:2"],
        },
        functions={"total": ["src/a.ts:f1", "src/a.ts:f2"], "covered": ["src/a.ts:f1"]},
    )
    second = copy.deepcopy(first)
    coordinates = second[kind]
    row = second["contribution"]
    assert isinstance(coordinates, dict) and isinstance(row, dict)
    if kind == "branches":
        coordinates["covered"] = ["src/a.ts:2", "src/a.ts:3"]
        row["removal_runs"][0]["covered_branches"] = ["src/a.ts:2", "src/a.ts:3"]
        row["removal_runs"][1].update(
            covered_branches=["src/a.ts:2"], unique_branches=["src/a.ts:3"]
        )
    else:
        coordinates["covered"] = ["src/a.ts:f2"]
    files = {"src/a.ts", "test/new.test.ts", "test/old.test.ts"}
    left, right = _coverage(first, files, ["src/**"]), _coverage(second, files, ["src/**"])
    left_contribution, right_contribution = left["test_contribution"], right["test_contribution"]
    assert isinstance(left_contribution, dict) and isinstance(right_contribution, dict)
    assert left_contribution.pop("semantic_digest") != right_contribution.pop("semantic_digest")
    assert left == right


def test_semantic_digest_ignores_set_order_timing_and_excluded_absent_vs_pending() -> None:
    first = _receipt()
    second = copy.deepcopy(first)
    row = second["contribution"]
    assert isinstance(row, dict)
    for key in ("reference_ids", "executed_ids", "passing_ids", "candidate_ids"):
        row[key].reverse()
    row["removal_runs"].reverse()
    for removal in row["removal_runs"]:
        for key in ("executed_ids", "passing_ids", "covered_branches", "unique_branches"):
            removal[key].reverse()
        removal["test_statuses"][removal["excluded_id"]] = "pending"
        removal.update(elapsed_seconds=999, command=["different scratch path"])
    assert _measure(first) == _measure(second)


def test_equal_counts_bind_which_native_reference_records_are_pending() -> None:
    reports = [_receipt(), _receipt()]
    for index, report in enumerate(reports):
        row = report["contribution"]
        assert isinstance(row, dict)
        other, extra = "test/old.test.ts::other", "test/old.test.ts::extra"
        pending, passing = (other, extra) if index == 0 else (extra, other)
        row["reference_ids"].append(extra)
        row["reference_statuses"].update({pending: "pending", passing: "passed"})
        for key in ("executed_ids", "passing_ids"):
            row[key].remove(other)
            row[key].append(passing)
        for removal in row["removal_runs"]:
            for key in ("executed_ids", "passing_ids"):
                removal[key].remove(other)
                removal[key].append(passing)
            removal["test_statuses"].update({pending: "pending", passing: "passed"})
    left, right = map(_measure, reports)
    assert left.pop("semantic_digest") != right.pop("semantic_digest")
    assert left == right


def test_excluded_file_cannot_supply_reference_coverage() -> None:
    report = _receipt()
    report["excluded_test_files"] = ["./test/old.test.ts"]
    with pytest.raises(ValueError, match="bound test scope"):
        _measure(report)


def test_removal_that_adds_coverage_is_not_zero_marginal() -> None:
    report = _receipt()
    contribution = report["contribution"]
    assert isinstance(contribution, dict)
    contribution["removal_runs"][0]["covered_branches"].append("src/a.ts:3")
    contribution["zero_marginal_ids"] = []
    assert _measure(report)["zero_marginal_count"] == 0
    contribution["zero_marginal_ids"] = ["test/new.test.ts::one"]
    with pytest.raises(ValueError, match="complete candidate population"):
        _measure(report)


@pytest.mark.parametrize("declared", [[], ["test/old.test.ts"], ["test/new.test.ts"]])
def test_changed_candidate_files_are_checked_against_the_bound_diff(
    tmp_path: Path, declared: list[str]
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    for root in (before, after):
        (root / "src").mkdir(parents=True)
        (root / "test").mkdir()
        (root / "src/a.ts").write_text("export const a = 1;\n")
        (root / "test/old.test.ts").write_text("old\n")
    (after / "test/new.test.ts").write_text("new\n")
    baseline = str(inspect_snapshot(before)["digest"])
    report = _receipt()
    report.update(
        before_digest=baseline,
        source_digest=inspect_snapshot(after)["digest"],
        files_measured=["src/a.ts"],
        branches={"total": ["src/a.ts:1", "src/a.ts:2"], "covered": ["src/a.ts:1", "src/a.ts:2"]},
        functions={"total": [], "covered": []},
    )
    contribution = report["contribution"]
    assert isinstance(contribution, dict)
    if declared != ["test/new.test.ts"]:
        # Internally self-consistent, but an omitted/substituted file must fail
        # even with an authentic baseline digest and bound report bytes.
        contribution.update(
            changed_test_files=declared, candidate_ids=[], removal_runs=[], zero_marginal_ids=[]
        )
        if declared:
            contribution["reference_ids"].remove("test/old.test.ts::other")
            contribution["reference_statuses"].pop("test/old.test.ts::other")
            contribution["executed_ids"].remove("test/old.test.ts::other")
            contribution["passing_ids"].remove("test/old.test.ts::other")
    config_path, report_path, envelope_path = [
        tmp_path / name for name in ("config.json", "report.json", "envelope.json")
    ]
    config_path.write_text(json.dumps({"test_scope": ["test/**"]}))
    report_path.write_text(json.dumps(report))
    envelope_path.write_text(
        json.dumps(
            bind_evidence(
                EvidenceBinding(after, report_path, config_path, "fixture", "1", ("src/**",))
            )
        )
    )
    stage = Stage(
        "handoff",
        Snapshot(after, str(inspect_snapshot(after)["digest"])),
        None,
        {"coverage": str(envelope_path)},
        None,
    )
    attempt = Attempt(
        "one",
        "task",
        1,
        "baseline",
        "fixture",
        Snapshot(before, baseline),
        (stage,),
        None,
        tmp_path,
        {},
    )
    if declared == ["test/new.test.ts"]:
        metrics = _stage(attempt, stage, None, tmp_path, {})["metrics"]
        assert isinstance(metrics, dict) and isinstance(metrics["coverage"], dict)
        coverage = metrics["coverage"]["metrics"]
        assert isinstance(coverage, dict) and isinstance(coverage["test_contribution"], dict)
        assert coverage["test_contribution"]["candidate_count"] == 2
    else:
        with pytest.raises(ValueError, match="changed-file selection differs"):
            _stage(attempt, stage, None, tmp_path, {})


@pytest.mark.parametrize(
    "fault",
    [
        "missing-run",
        "omitted-candidate",
        "unexecuted-reference",
        "failed-removal",
        "wrong-remaining",
        "wrong-lost",
        "scope-escape",
        "missing-native-statuses",
        "wrong-native-status",
        "excluded-passed",
    ],
)
def test_incomplete_or_unbound_contribution_is_not_zero(fault: str) -> None:
    report = copy.deepcopy(_receipt())
    contribution = report["contribution"]
    assert isinstance(contribution, dict)
    if fault == "missing-run":
        contribution["removal_runs"].pop()
    elif fault == "omitted-candidate":
        contribution["candidate_ids"].pop()
    elif fault == "unexecuted-reference":
        contribution["executed_ids"].pop()
    elif fault == "failed-removal":
        contribution["removal_runs"][0]["exit_code"] = 1
    elif fault == "wrong-remaining":
        contribution["removal_runs"][0]["passing_ids"].pop()
    elif fault == "wrong-lost":
        contribution["removal_runs"][1]["unique_branches"] = []
    elif fault == "missing-native-statuses":
        contribution["removal_runs"][0].pop("test_statuses")
    elif fault == "wrong-native-status":
        contribution["removal_runs"][0]["test_statuses"]["test/new.test.ts::two"] = "pending"
    elif fault == "excluded-passed":
        contribution["removal_runs"][0]["test_statuses"]["test/new.test.ts::one"] = "passed"
    else:
        report["test_scope"] = ["other/**"]
    with pytest.raises(ValueError):
        _measure(report)


@pytest.mark.parametrize("mutate_before", [False, True])
def test_collector_receives_disposable_bound_before_context(
    tmp_path: Path, mutate_before: bool
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    for root, text in ((before, "before"), (after, "after")):
        (root / "src").mkdir(parents=True)
        (root / "src/a.js").write_text(text)
    script = tmp_path / "measure.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "base=pathlib.Path(sys.argv[1])\n"
        "assert (base/'src/a.js').read_text() == 'before'\n"
        + ("(base/'src/a.js').write_text('mutated')\n" if mutate_before else "")
        + "pathlib.Path(sys.argv[2]).write_text(json.dumps({'files_measured':['src/a.js'],'branches':{'total':[],'covered':[]},'functions':{'total':[],'covered':[]}}))\n"
    )
    config = tmp_path / "config.json"
    config.write_text("{}")
    collector = Collector(
        kind="coverage",
        config=config,
        command=[sys.executable, str(script), "{before}", "{output}"],
        tool_version="fixture",
    )
    evidence = tmp_path / "evidence"
    if mutate_before:
        with pytest.raises(ValueError, match="^collector modified its before snapshot$"):
            collect_measurements(after, evidence, [collector], before=before)
        assert (evidence / "coverage/execution.json").is_file()
        assert not (evidence / "coverage/envelope.json").exists()
        assert not (evidence / "coverage/failure.json").exists()
    else:
        assert "coverage" in collect_measurements(after, evidence, [collector], before=before)
    assert (before / "src/a.js").read_text() == "before"
    assert (after / "src/a.js").read_text() == "after"
