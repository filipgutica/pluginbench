from __future__ import annotations

import copy
import csv
import io
import json
import re
import subprocess
from pathlib import Path

import pytest

from pluginbench.assessment_charts import chart_artifacts_from_data, chart_data


def _report() -> dict[str, object]:
    """Synthetic canonical-shaped observations, used only to exercise chart mapping."""
    provenance = {
        "tool": "fixture",
        "tool_version": "1",
        "config_digest": "config",
        "scope": ["src/**", "test/**"],
    }
    metrics = {
        "fallow": {
            "status": "incomplete",
            "provenance": provenance,
            "metrics": {
                "max_cognitive_reported": 4,
                "max_cyclomatic_reported": 5,
                "complexity_complete_for_declared_scope": True,
                "dead_code": 0,
                "dead_code_complete_for_declared_scope": False,
            },
        },
        "coverage": {
            "status": "available",
            "provenance": provenance,
            "metrics": {
                "branch_percent": 75,
                "function_percent": 100,
                "population_complete_for_declared_scope": True,
                "files_measured": ["src/fixture.ts", "test/fixture.test.ts"],
                "branch_denominator_digest": "branches",
                "function_denominator_digest": "functions",
                "per_test_count": 3,
                "zero_unique_branch_contribution_tests": ["one", "two"],
            },
        },
    }
    stage = {
        "id": "handoff",
        "snapshot_digest": "snapshot",
        "snapshot_metadata": {"source_lines": 20, "test_lines": 30, "exclusion_names": [".git"]},
        "change": {
            "source_lines_added": 2,
            "source_lines_removed": 1,
            "test_lines_added": 5,
            "test_lines_removed": 0,
        },
        "work": {
            "tokens": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_output_tokens": 7,
            },
            "token_format": "native codex",
            "elapsed_seconds": 5,
        },
        "correctness": {"status": "pass"},
        "metrics": metrics,
    }
    attempts = [
        {
            "id": arm,
            "task_id": "fixture-task",
            "arm": arm,
            "model": "exact-model",
            "provenance": {
                "host": "codex",
                "harness": "codex-cli",
                "harness_version": "fixture-1",
                "reasoning_effort": "medium",
            },
            "before_digest": "seed",
            "handoff": "handoff",
            "stages": [copy.deepcopy(stage)],
        }
        for arm in ("baseline", "candidate")
    ]
    return {
        "schema_version": 1,
        "name": "Synthetic chart tests",
        "snapshot_roots": [],
        "attempts": attempts,
    }


def _attempt(report: dict[str, object], index: int = 1) -> dict[str, object]:
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    row = attempts[index]
    assert isinstance(row, dict)
    return row


def test_judge_tokens_remain_separate_and_require_matching_observed_identity() -> None:
    report = _report()
    report["independent_review"] = {"status": "complete"}
    for index in (0, 1):
        _attempt(report, index)["independent_review"] = {
            "stage": "handoff",
            "stage_id": "handoff",
            "criteria_sha256": "a" * 64,
            "work": {
                "tokens": {"input_tokens": 30, "output_tokens": 7},
                "token_format": "native codex",
            },
            "execution": {
                "actual_primary_models": ["judge-A"],
                "runtime_effort_evidence": ["high"],
                "startup": [{"type": "session_meta", "payload": {"cli_version": "fixture-1"}}],
            },
        }
    data = chart_data(report)
    rows = _observations(data, "Judge input tokens")
    assert [row["value"] for row in rows] == [30, 30]
    assert [row["value"] for row in _observations(data, "Input tokens")] == [100, 100]
    assert all(row["compatibility"] for row in rows)
    review = _attempt(report)["independent_review"]
    assert isinstance(review, dict) and isinstance(review["execution"], dict)
    review["execution"]["actual_primary_models"] = ["judge-B"]
    assert (
        next(
            row
            for row in _observations(chart_data(report), "Judge input tokens")
            if row["arm"] == "candidate"
        )["status"]
        == "not comparable"
    )


def _stage(report: dict[str, object], index: int = 1) -> dict[str, object]:
    stages = _attempt(report, index)["stages"]
    assert isinstance(stages, list) and isinstance(stages[0], dict)
    return stages[0]


@pytest.mark.parametrize("field", ["actual_primary_models", "runtime_effort_evidence"])
def test_conflicting_judge_identity_stays_raw_even_when_both_arms_agree(field: str) -> None:
    report = _report()
    report["independent_review"] = {"status": "incomplete"}
    for index in (0, 1):
        execution: dict[str, object] = {
            "actual_primary_models": ["judge-A"],
            "runtime_effort_evidence": ["high"],
            "startup": [{"type": "session_meta", "payload": {"cli_version": "fixture-1"}}],
        }
        execution[field] = ["first", "second"]
        _attempt(report, index)["independent_review"] = {
            "stage": "handoff",
            "stage_id": "handoff",
            "criteria_sha256": "a" * 64,
            "work": {"tokens": {"input_tokens": 50}, "token_format": "native codex"},
            "execution": execution,
        }
    data = chart_data(report)
    assert not _matched(data, "Judge input tokens")
    assert all(row["value"] == 50 for row in _observations(data, "Judge input tokens"))


def _chart_rows(data: dict[str, object], key: str) -> list[dict[str, object]]:
    rows = data[key]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    return [row for row in rows if isinstance(row, dict)]


def _measurement(report: dict[str, object], kind: str) -> dict[str, object]:
    metrics = _stage(report)["metrics"]
    assert isinstance(metrics, dict)
    value = metrics[kind]
    assert isinstance(value, dict)
    return dict(value)


def _metric_values(report: dict[str, object], kind: str) -> dict[str, object]:
    values = _measurement(report, kind)["metrics"]
    assert isinstance(values, dict)
    return values


def _observations(data: dict[str, object], metric: str) -> list[dict[str, object]]:
    rows = data["observations"]
    assert isinstance(rows, list)
    return [
        row
        for row in rows
        if isinstance(row, dict) and row["role"] == "handoff" and row["metric"] == metric
    ]


def _matched(data: dict[str, object], metric: str) -> list[dict[str, object]]:
    rows = data["medians"]
    assert isinstance(rows, list)
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and row["role"] == "handoff"
        and row["arm"] == "candidate"
        and row["metric"] == metric
    ]


def _empty_baseline_population_report() -> dict[str, object]:
    report = _report()
    for index, count, cognitive, cyclomatic in ((0, 0, None, None), (1, 3, 14, 11)):
        _stage(report, index)["metric_comparison"] = {
            "fallow": {
                "status": "available",
                "new_file_functions": {
                    "status": "available",
                    "method": "paths absent before",
                    "after": {
                        "function_count_reported": count,
                        "max_cognitive_reported": cognitive,
                        "max_cyclomatic_reported": cyclomatic,
                        "complete_for_declared_scope": True,
                    },
                },
            }
        }
    return report


def test_raw_medians_keep_numeric_guidance_when_baseline_population_is_empty() -> None:
    data = chart_data(_empty_baseline_population_report())
    for metric, value in (
        ("New-file function cognitive maximum", 14),
        ("New-file function cyclomatic maximum", 11),
    ):
        rows = [row for row in _chart_rows(data, "raw_medians") if row["metric"] == metric]
        assert len(rows) == 1 and rows[0]["arm"] == "candidate" and rows[0]["value"] == value
        assert rows[0]["measured"] == 1 and rows[0]["baseline_measured"] == 0
        assert rows[0]["attempt_ids"] == ["candidate"]
        assert not _matched(data, metric)
        assert _observations(data, metric)[0]["value"] is None


def test_raw_medians_do_not_pool_incompatible_or_unknown_repeat_profiles() -> None:
    report = _empty_baseline_population_report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    second = copy.deepcopy(_attempt(report, 1))
    second["id"] = "candidate-2"
    attempts.append(second)
    metrics = _stage(report, 2)["metrics"]
    assert isinstance(metrics, dict)
    metrics["fallow"]["provenance"]["tool_version"] = "different"
    metric = "New-file function cognitive maximum"
    for unknown in (False, True):
        if unknown:
            for index in (1, 2):
                provenance = _attempt(report, index)["provenance"]
                assert isinstance(provenance, dict)
                provenance["reasoning_effort"] = "unknown"
        rows = [
            row for row in _chart_rows(chart_data(report), "raw_medians") if row["metric"] == metric
        ]
        assert len(rows) == 2 and all(row["measured"] == 1 for row in rows)
        assert len({row["raw_profile_id"] for row in rows}) == 2
        assert [row["attempt_ids"] for row in rows] == [["candidate"], ["candidate-2"]]


def test_chart_omits_full_judge_records_without_mutating_canonical_evidence() -> None:
    report = _report()
    summary = {"status": "complete", "stage": "handoff", "requested_reasoning_effort": "high"}
    report["independent_review"] = {**summary, "records": [{"transcript": "x" * 100_000}]}
    original = copy.deepcopy(report)
    data = chart_data(report)
    assert data["independent_review"] == summary
    assert report == original
    without_records = copy.deepcopy(report)
    without_records["independent_review"] = summary
    assert data == chart_data(without_records)


def test_compact_syntax_provenance_retains_full_binding_compatibility() -> None:
    report = _report()
    syntax = {"parser": "oxc", "version": "1", "dependency_hashes": {"parser": "original"}}
    for index in (0, 1):
        metrics = _stage(report, index)["metrics"]
        assert isinstance(metrics, dict)
        provenance = metrics["fallow"]["provenance"]
        provenance["syntax_observer"] = copy.deepcopy(syntax)
    original = copy.deepcopy(report)
    data = chart_data(report)
    assert _matched(data, "Reported cognitive maximum")
    rows = _observations(data, "Reported cognitive maximum")
    assert [row["value"] for row in rows] == [4, 4]
    assert _matched(data, "Reported cognitive maximum")[0]["measured"] == 1
    for row in rows:
        assert isinstance(row["compatibility"], str) and len(row["compatibility"]) == 64
        provenance = row["provenance"]
        assert isinstance(provenance, dict)
        compact = provenance["syntax_observer"]
        assert isinstance(compact, dict)
        assert "dependency_hashes" not in compact and compact["binding_digest"]
    assert report == original
    metrics = _stage(report)["metrics"]
    assert isinstance(metrics, dict)
    metrics["fallow"]["provenance"]["syntax_observer"]["dependency_hashes"]["parser"] = "changed"
    changed = chart_data(report)
    assert not _matched(changed, "Reported cognitive maximum")
    assert [row["value"] for row in _observations(changed, "Reported cognitive maximum")] == [4, 4]


def test_compact_loc_inventories_do_not_change_scope_based_comparison() -> None:
    report = _report()
    for index in (0, 1):
        _stage(report, index)["code_size"] = {
            "status": "available",
            "method": "scoped-source-lines-v1",
            "scope": ["src/**"],
            "after_source_lines": 20 + index,
            "source_lines_added": 2,
            "source_lines_removed": 1,
            "before_paths": ["src/index.ts"],
            "after_paths": ["src/index.ts"] + (["src/new.ts"] if index else []),
            "before_digest": "seed",
            "after_digest": "snapshot",
        }
    original = copy.deepcopy(report)
    data = chart_data(report)
    rows = _observations(data, "Source LOC")
    assert [row["value"] for row in rows] == [20, 21]
    assert _matched(data, "Source LOC")[0]["measured"] == 1
    for index, row in enumerate(rows):
        provenance = row["provenance"]
        assert isinstance(provenance, dict)
        assert "after_paths" not in provenance and provenance["after_file_count"] == 1 + index
        assert provenance["after_inventory_digest"]
    assert report == original


def _controller_result(data: dict[str, object], tmp_path: Path) -> dict[str, object]:
    """Run the current evaluation completeness note without a browser."""
    template = Path("src/pluginbench/assessment-charts.html").read_text()
    controller = template.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    note = re.search(r"function complexityNote\(parent, rows\) \{.*?\n\}", controller, re.S)
    assert note is not None
    script = tmp_path / "controller.js"
    stub = r"""
const unique = items => [...new Set(items)];
const armLabel = arm => arm === 'baseline' ? 'Vanilla' : arm;
const create = (tag, text, parent) => { const node = {textContent:text}; parent.append(node); return node; };
"""
    capture = r"""
const notes = [];
complexityNote({append: node => notes.push(node.textContent)}, data.observations.filter(row => row.panel === 'Complexity'));
console.log(JSON.stringify({complexity_note: notes.join(' ')}));
"""
    script.write_text(
        stub
        + "\nconst data = "
        + json.dumps({**data, "kind": "evaluation"})
        + ";\n"
        + note.group(0)
        + capture
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return dict(parsed)


@pytest.mark.parametrize("completeness", [None, "missing", False])
def test_controller_complexity_note_keeps_unknown_distinct_from_partial(
    tmp_path: Path, completeness: object
) -> None:
    data = chart_data(_report())
    observations = data["observations"]
    assert isinstance(observations, list)
    for row in observations:
        if row["panel"] == "Complexity":
            provenance = row["provenance"]
            if completeness == "missing":
                provenance.pop("complete_for_declared_scope")
            else:
                provenance["complete_for_declared_scope"] = completeness
    result = _controller_result(data, tmp_path)
    note = str(result["complexity_note"])
    if completeness is False:
        assert "partial declared scan" in note and "scan completeness unknown" not in note
    else:
        assert "scan completeness unknown" in note and "partial declared scan" not in note


def test_native_subsets_are_not_added_and_loc_uses_snapshot_totals() -> None:
    data = chart_data(_report())
    assert [row["value"] for row in _observations(data, "Input tokens")] == [100, 100]
    assert [row["value"] for row in _observations(data, "Output tokens")] == [10, 10]
    assert [row["value"] for row in _observations(data, "Source LOC")] == [20, 20]
    assert [row["value"] for row in _observations(data, "Test LOC")] == [30, 30]
    assert _matched(data, "Reported cognitive maximum")[0]["value"] == 4
    assert not _matched(data, "Reported dead findings")
    dead = _observations(data, "Reported dead findings")[0]
    assert dead["value"] == 0 and "not absence" in str(dead["reason"])


@pytest.mark.parametrize(
    "field,value",
    [("task_id", "other"), ("model", "other"), ("before_digest", "other"), ("arm", None)],
)
def test_task_model_seed_and_explicit_arm_are_required_for_matching(
    field: str, value: object
) -> None:
    report = _report()
    _attempt(report)[field] = value
    assert not _matched(chart_data(report), "Input tokens")


def test_host_and_native_token_format_mismatch_cannot_be_pooled() -> None:
    report = _report()
    _attempt(report)["provenance"] = {"host": "claude"}
    assert not _matched(chart_data(report), "Input tokens")
    report = _report()
    _stage(report)["work"] = {
        "tokens": {"input_tokens": 100},
        "token_format": "different provider accounting",
    }
    assert not _matched(chart_data(report), "Input tokens")


@pytest.mark.parametrize("field", ["host", "harness", "harness_version", "reasoning_effort"])
def test_unknown_runtime_in_both_arms_retains_raw_values_without_comparing(field: str) -> None:
    report = _report()
    for index in (0, 1):
        provenance = _attempt(report, index)["provenance"]
        assert isinstance(provenance, dict)
        provenance.pop(field)
    data = chart_data(report)
    assert not _matched(data, "Input tokens")
    assert not _matched(data, "Reported cognitive maximum")
    assert all(
        row["value"] == 100 and row["status"] == "not comparable"
        for row in _observations(data, "Input tokens")
    )


def test_task_quality_charts_keep_raw_populations_and_partial_or_missing_evidence() -> None:
    report = _report()
    population = {
        "function_count_reported": 2,
        "max_cognitive_reported": 20,
        "max_cyclomatic_reported": 4,
        "max_unit_size_reported": 12,
        "complete_for_declared_scope": True,
    }
    comparison = {
        "status": "available",
        "changed_functions": {
            "status": "available",
            "before": population,
            "after": {**population, "function_count_reported": 1, "max_cognitive_reported": 10},
            "method": "bound source hunk overlap",
            "limitations": "not matched functions",
        },
        "new_file_functions": {
            "status": "available",
            "after": population,
            "method": "paths absent before",
        },
        "dead_findings": {
            "status": "available",
            "introduced_count": 1,
            "resolved_count": 1,
            "method": "declaration identity",
        },
    }
    for index in (0, 1):
        _stage(report, index)["metric_comparison"] = {"fallow": copy.deepcopy(comparison)}
    partial_comparison = _stage(report, 1)["metric_comparison"]
    assert isinstance(partial_comparison, dict)
    partial_fallow = partial_comparison["fallow"]
    assert isinstance(partial_fallow, dict)
    partial = partial_fallow["changed_functions"]
    assert isinstance(partial, dict)
    partial["status"] = "incomplete"
    partial["after"]["complete_for_declared_scope"] = False
    partial_fallow["dead_findings"] = {
        "status": "unavailable",
        "introduced_count": None,
        "resolved_count": None,
    }
    data = chart_data(report)
    before = _observations(data, "Before changed-function count")
    after = _observations(data, "After changed-function count")
    assert [row["value"] for row in before] == [2, 2]
    assert [row["value"] for row in after] == [1, 1]
    assert after[0]["status"] == "measured" and after[1]["status"] == "not comparable"
    reason = after[1]["reason"]
    assert isinstance(reason, str)
    assert "population incomplete" in reason
    assert _observations(data, "After changed-function cognitive maximum")[1]["value"] == 10
    dead = _observations(data, "Introduced dead findings")
    assert dead[0]["value"] == 1 and dead[1]["value"] is None
    assert dead[1]["status"] == "unknown"
    _, csv_text = chart_artifacts_from_data(chart_data(report))
    exported = list(csv.DictReader(io.StringIO(csv_text)))
    row = next(
        row
        for row in exported
        if row["metric"] == "After changed-function cognitive maximum" and row["arm"] == "candidate"
    )
    assert row["value"] == "10.0" and row["status"] == "not comparable"
    assert "not matched functions" in json.loads(row["provenance"])["limitations"]


@pytest.mark.parametrize(
    "field,value", [("tool_version", "2"), ("config_digest", "other"), ("scope", ["src/**"])]
)
def test_analyzer_provenance_mismatch_retains_unpaired_points(field: str, value: object) -> None:
    report = _report()
    provenance = _measurement(report, "coverage")["provenance"]
    assert isinstance(provenance, dict)
    provenance[field] = value
    data = chart_data(report)
    assert not _matched(data, "Branch coverage %")
    assert [row["value"] for row in _observations(data, "Branch coverage %")] == [75, 75]


def test_coverage_rates_describe_each_complete_submission_despite_different_denominators() -> None:
    report = _report()
    _metric_values(report, "fallow")["complexity_complete_for_declared_scope"] = False
    _metric_values(report, "coverage")["branch_denominator_digest"] = "changed coordinates"
    data = chart_data(report)
    assert not _matched(data, "Reported cognitive maximum")
    assert _matched(data, "Branch coverage %")
    assert _matched(data, "Function coverage %")
    candidate = next(
        row for row in _observations(data, "Branch coverage %") if row["arm"] == "candidate"
    )
    assert "descriptive" in str(candidate["reason"])
    assert "no fixed-denominator gain" in str(candidate["reason"])


def test_controlled_coverage_clock_is_disclosed_and_separates_comparisons() -> None:
    report = _report()
    clock = {"policy": "stepped-date-v1", "epoch_ms": 0, "step_ms": 1}
    for index in (0, 1):
        measurements = _stage(report, index)["metrics"]
        assert isinstance(measurements, dict)
        coverage = measurements["coverage"]
        assert isinstance(coverage, dict) and isinstance(coverage["metrics"], dict)
        coverage["metrics"]["clock"] = copy.deepcopy(clock)
    data = chart_data(report)
    rows = _observations(data, "Branch coverage %")
    assert [row["value"] for row in rows] == [75, 75]
    assert _matched(data, "Branch coverage %")
    for row in rows:
        provenance = row["provenance"]
        assert isinstance(provenance, dict) and provenance["clock"] == clock
    assert all("controlled Date clock" in str(row["reason"]) for row in rows)
    _metric_values(report, "coverage")["clock"] = {**clock, "step_ms": 2}
    assert not _matched(chart_data(report), "Branch coverage %")


def test_missing_failed_submission_remains_assigned_with_unknown_metrics() -> None:
    report = _report()
    _stage(report).update(
        snapshot_digest=None,
        snapshot_metadata=None,
        metrics=None,
        change=None,
        work=None,
        correctness={"status": "fail"},
    )
    data = chart_data(report)
    assert data["assigned"] == 2
    candidate = [row for row in _observations(data, "Source LOC") if row["arm"] == "candidate"][0]
    assert candidate["value"] is None and candidate["correctness"] == "fail"
    assert not _matched(data, "Source LOC")


def test_scoped_loc_and_raw_generated_fallback_do_not_pool() -> None:
    report = _report()
    _stage(report)["code_size"] = {
        "status": "available",
        "method": "scoped-source-lines-v1",
        "scope": ["src/**"],
        "after_source_lines": 4,
        "source_lines_added": 2,
        "source_lines_removed": 0,
        "before_paths": ["src/index.ts"],
        "after_paths": ["src/index.ts"],
        "before_digest": "seed",
        "after_digest": "snapshot",
        "counting_semantics": "text lines",
    }
    _stage(report)["change"] = {"source_lines_added": 22, "source_lines_removed": 0}
    data = chart_data(report)
    candidate = next(row for row in _observations(data, "Source LOC") if row["arm"] == "candidate")
    baseline = next(row for row in _observations(data, "Source LOC") if row["arm"] == "baseline")
    assert candidate["value"] == 4 and baseline["value"] == 20
    assert candidate["source_lines_added"] == 2
    change_provenance = candidate["change_loc_provenance"]
    assert isinstance(change_provenance, dict)
    assert change_provenance["raw_extension_classified_counts"]["source_lines_added"] == 22
    assert "src/**" in str(candidate["reason"])
    assert "including generated files" in str(baseline["reason"])
    assert not _matched(data, "Source LOC")
    _stage(report)["code_size"] = {"status": "unavailable", "reason": "scopes disagree"}
    candidate = next(
        row for row in _observations(chart_data(report), "Source LOC") if row["arm"] == "candidate"
    )
    assert candidate["value"] is None
    assert "scopes disagree" in str(candidate["reason"])
    assert candidate["source_lines_added"] is None


def test_coverage_without_declared_population_proof_stays_raw() -> None:
    report = _report()
    _metric_values(report, "coverage").pop("population_complete_for_declared_scope")
    data = chart_data(report)
    assert not _matched(data, "Branch coverage %")
    row = next(row for row in _observations(data, "Branch coverage %") if row["arm"] == "candidate")
    assert row["value"] == 75 and "population completeness is unproven" in str(row["reason"])


@pytest.mark.parametrize("missing,empty", [(True, False), (False, False), (False, True)])
def test_public_coverage_observation_preserves_lcov_ambiguity_reason(
    missing: bool, empty: bool
) -> None:
    report = _report()
    coverage = _metric_values(report, "coverage")
    reason = "Repeated LCOV function names have mixed hits; FNDA has no source line"
    coverage.update(
        function_population_complete=False,
        unavailable_populations={"functions": {"files": ["src/fixture.ts"], "reason": reason}},
    )
    if missing:
        coverage.update(function_percent=None, function_denominator_digest=None)
    if empty:
        coverage.update(function_percent=None, function_total=0)
    data = chart_data(report)
    row = next(
        row for row in _observations(data, "Function coverage %") if row["arm"] == "candidate"
    )
    assert row["reason"] == reason + ("; unknown is not zero" if missing or empty else "")
    assert not _matched(data, "Function coverage %")


@pytest.mark.parametrize(
    "actual,version,correctness,state",
    [
        (None, None, "fail", "complete"),
        (None, None, "unknown", "pending"),
        ("wrong-model", "0.162.0", "fail", "complete"),
        ("exact-model", "9.9.9", "fail", "complete"),
    ],
)
def test_failed_or_mismatched_execution_retains_assignment_and_raw_runtime_evidence(
    tmp_path: Path, actual: str | None, version: str | None, correctness: str, state: str
) -> None:
    report = _report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    failed = copy.deepcopy(_attempt(report))
    failed["id"] = "failed-launch"
    failed["model"] = actual
    failed_stages = failed["stages"]
    assert isinstance(failed_stages, list) and isinstance(failed_stages[0], dict)
    failed_stages[0].update(snapshot_digest=None, work=None, correctness={"status": correctness})
    attempts.append(failed)
    for attempt in attempts:
        provenance = attempt["provenance"]
        assert isinstance(provenance, dict)
        provenance.update(
            input="codex-study",
            assigned_model="exact-model",
            harness="codex-cli",
            harness_version="0.162.0" if attempt["id"] != "failed-launch" else version,
            assigned_harness_version="0.162.0",
            requested_reasoning_effort="medium",
            reasoning_effort=None
            if attempt["id"] == "failed-launch" and actual is None
            else "medium",
            attempt_status=state if attempt["id"] == "failed-launch" else "complete",
        )
        if attempt["id"] == "failed-launch" and state == "pending":
            provenance["assigned_reasoning_effort"] = "medium"
            provenance.pop("requested_reasoning_effort")
            provenance.pop("reasoning_effort")
    data = chart_data(report)
    assert data["assigned"] == 3
    failed_metric = next(
        row for row in _chart_rows(data, "observations") if row["attempt_id"] == "failed-launch"
    )
    assert failed_metric["model"] == (actual or "unknown")  # Metrics keep actual identities.
    assert failed_metric["harness_version"] == (version or "unknown")
    assert failed_metric["compatibility"] is None
    if state == "pending":
        assert failed_metric["requested_reasoning_effort"] == "unknown"
        assert failed_metric["reasoning_effort"] == "unknown"
    completeness = [
        row
        for row in _chart_rows(data, "measurement_completeness")
        if row["arm"] == "candidate" and row["role"] == "handoff"
    ]
    assert len(completeness) == 2
    assert all(
        row["model"] == "exact-model" and row["harness_version"] == "0.162.0"
        for row in completeness
    )
    if actual is not None:
        failed_stages[0]["work"] = _stage(report)["work"]
        partial_tokens = next(
            row
            for row in _chart_rows(chart_data(report), "observations")
            if row["attempt_id"] == "failed-launch" and row["metric"] == "Input tokens"
        )
        assert partial_tokens["value"] == 100
        assert partial_tokens["compatibility"] is None


@pytest.mark.parametrize(
    "population,names,expected", [(3, ["one", "two"], 2), (None, None, None), (0, [], None)]
)
def test_zero_unique_signals_are_among_supplied_tests_and_never_redundancy_claims(
    population: object, names: object, expected: object
) -> None:
    report = _report()
    _metric_values(report, "coverage").update(
        per_test_count=population, zero_unique_branch_contribution_tests=names
    )
    data = chart_data(report)
    row = [row for row in _observations(data, "Branch contribution") if row["arm"] == "candidate"][
        0
    ]
    assert row["value"] == expected
    assert not _matched(data, "Branch contribution")


@pytest.mark.parametrize("candidate_scope", [["test/**"], ["integration/**"], None])
def test_test_overlap_requires_matching_declared_test_scope(candidate_scope: object) -> None:
    report = _report()
    for index, scope in ((0, ["test/**"]), (1, candidate_scope)):
        stages = _attempt(report, index)["stages"]
        assert isinstance(stages, list) and isinstance(stages[0], dict)
        metrics = stages[0]["metrics"]
        assert isinstance(metrics, dict) and isinstance(metrics["coverage"], dict)
        coverage = metrics["coverage"]["metrics"]
        assert isinstance(coverage, dict)
        coverage.update(
            test_scope=scope,
            test_contribution={
                "status": "available",
                "method": "leave-one-out-branches-v1",
                "selection": "all runnable cases in added/modified visible test files",
                "candidate_count": 2,
                "tested_count": 2,
                "zero_marginal_count": 1,
            },
        )
    data = chart_data(report)
    rows = _observations(data, "Zero marginal branch contribution cases")
    assert all(
        isinstance(row["provenance"], dict) and row["provenance"]["test_scope"] is not None
        for row in rows
        if row["status"] == "measured"
    )
    assert bool(_matched(data, "Zero marginal branch contribution cases")) is (
        candidate_scope == ["test/**"]
    )


def test_complete_capture_accepts_zero_counts_and_rejects_a_missing_denominator() -> None:
    report = _report()
    stage = _stage(report)
    stage.update(
        code_size={"status": "available", "source_lines_added": 0, "source_lines_removed": 0},
        test_code_size={"status": "available", "test_lines_added": 0, "test_lines_removed": 0},
        metric_comparison={
            "fallow": {
                "dead_findings": {"status": "available", "introduced_count": 0},
                "clone_findings": {"status": "available", "introduced_count": 0},
            }
        },
    )
    _metric_values(report, "fallow").update(function_count_reported=0)
    coverage = _metric_values(report, "coverage")
    coverage.update(
        branch_total=0,
        branch_covered=0,
        function_total=0,
        function_covered=0,
        test_contribution={
            "status": "available",
            "candidate_count": 0,
            "tested_count": 0,
            "zero_marginal_count": 0,
        },
    )
    row = next(
        row
        for row in _chart_rows(chart_data(report), "measurement_completeness")
        if row["arm"] == "candidate" and row["role"] == "handoff"
    )
    assert row["complete"] is True
    coverage.pop("function_total")
    row = next(
        row
        for row in _chart_rows(chart_data(report), "measurement_completeness")
        if row["arm"] == "candidate" and row["role"] == "handoff"
    )
    assert row["complete"] is False
    assert (
        isinstance(row["counts"], dict) and row["counts"]["Coverage covered/total counts"] is False
    )


@pytest.mark.parametrize("all_failed", [False, True])
def test_complete_capture_retains_failed_configured_observers(all_failed: bool) -> None:
    report = _report()
    for index in (0, 1) if all_failed else (1,):
        attempt = _attempt(report, index)
        attempt["configured_metrics"] = ["fallow", "coverage"]
        _stage(report, index)["metrics"] = {
            "fallow": {"status": "unavailable", "metrics": None},
            "coverage": {
                "status": "unavailable",
                "metrics": None,
                "reason": "coverage command timed out",
            },
        }
    coverage_rows = _observations(chart_data(report), "Branch coverage %")
    failed_coverage = next(row for row in coverage_rows if row["arm"] == "candidate")
    assert failed_coverage["value"] is None
    assert failed_coverage["reason"] == "coverage command timed out"
    rows = [
        row
        for row in _chart_rows(chart_data(report), "measurement_completeness")
        if row["role"] == "handoff"
    ]
    assert {row["attempt_id"] for row in rows} == {"baseline", "candidate"}
    failed = next(row for row in rows if row["arm"] == "candidate")
    assert failed["complete"] is False
    assert isinstance(failed["counts"], dict)
    assert failed["counts"]["Complexity population"] is False
    assert failed["counts"]["Coverage covered/total counts"] is False


def test_untrusted_labels_cannot_inject_html_or_spreadsheet_formulas() -> None:
    report = _report()
    _attempt(report)["id"] = "=cmd()"
    _attempt(report)["task_id"] = "</script><script>alert(1)</script>"
    html, csv_text = chart_artifacts_from_data(chart_data(report))
    assert "</script><script>alert" not in html
    assert "\\u003c/script>" in html
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert any(row["attempt_id"] == "'=cmd()" for row in rows)
    controller = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "textContent" in controller and "innerHTML" not in controller
    assert re.search(r"<script[^>]+src=", html) is None
    assert "__ECHARTS_SCRIPT__" not in html and "echarts.init" in controller


@pytest.mark.parametrize(
    "setting", ["harness", "harness_version", "reasoning_effort", "requested_reasoning_effort"]
)
def test_runtime_settings_are_kept_in_separate_comparison_groups(setting: str) -> None:
    report = _report()
    provenance = _attempt(report)["provenance"]
    assert isinstance(provenance, dict)
    provenance[setting] = "different"
    data = chart_data(report)
    medians = data["medians"]
    assert isinstance(medians, list)
    assert not any(row["arm"] == "candidate" for row in medians)


def _fixed_review_report() -> dict[str, object]:
    report = _report()
    report["independent_review"] = {
        "status": "complete",
        "stage": "handoff",
        "criteria_sha256": "sha256:" + "a" * 64,
        "revision": "fixed-review-v1",
    }
    for index in (0, 1):
        _attempt(report, index)["independent_review"] = {
            "status": "reviewed",
            "stage": "handoff",
            "stage_id": "handoff",
            "criteria_sha256": "sha256:" + "a" * 64,
            "requested_model": "judge-A",
            "requested_reasoning_effort": "high",
            "execution": {
                "actual_primary_models": ["judge-A"],
                "runtime_effort_evidence": ["high"],
                "startup": [{"type": "session_meta", "payload": {"cli_version": "fixture-1"}}],
            },
            "issues": [],
            "finding_count": 0,
            "severity_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "category_counts": {"correctness": 0, "scope": 0, "maintainability": 0, "tests": 0},
        }
    return report


def _fixed_review(report: dict[str, object], index: int = 1) -> dict[str, object]:
    review = _attempt(report, index)["independent_review"]
    assert isinstance(review, dict)
    return review


def test_fixed_review_counts_preserve_valid_zero_without_requiring_token_usage() -> None:
    data = chart_data(_fixed_review_report())
    for metric in (
        "Total review findings",
        "P0 review findings",
        "P1 review findings",
        "P2 review findings",
        "P3 review findings",
        "Correctness review findings",
        "Scope review findings",
        "Maintainability review findings",
        "Tests review findings",
    ):
        assert [row["value"] for row in _observations(data, metric)] == [0, 0]
        assert len(_matched(data, metric)) == 1
    assert all(row["value"] is None for row in _observations(data, "Judge input tokens"))
    assert [row["value"] for row in _observations(data, "Input tokens")] == [100, 100]


def test_review_counts_match_each_issue_once_and_expose_findings_once() -> None:
    report = _fixed_review_report()
    review = _fixed_review(report)
    review["issues"] = [
        {
            "issue_id": f"issue-{index}",
            "severity": severity,
            "category": category,
            "description": "Concrete issue",
            "rationale": "Evidence-backed impact",
            "path": "src/fixture.ts",
            "line": index + 1,
            "evidence": "source excerpt",
            "smallest_fix": "Bounded correction",
        }
        for index, (severity, category) in enumerate(
            zip(("P0", "P1", "P2", "P3"), ("correctness", "scope", "maintainability", "tests"))
        )
    ]
    review.update(
        finding_count=4,
        severity_counts={"P0": 1, "P1": 1, "P2": 1, "P3": 1},
        category_counts={"correctness": 1, "scope": 1, "maintainability": 1, "tests": 1},
    )
    data = chart_data(report)
    assert [row["value"] for row in _observations(data, "Total review findings")] == [0, 4]
    for metric in ("P0 review findings", "P2 review findings", "Scope review findings"):
        assert [row["value"] for row in _observations(data, metric)] == [0, 1]
    records = _chart_rows(data, "review_findings")
    assert len(records) == 2 and records[1]["issues"] == review["issues"]
    for row in _observations(data, "Total review findings"):
        provenance = row["provenance"]
        assert isinstance(provenance, dict) and "issues" not in provenance
    review["severity_counts"] = {"P0": 0, "P1": 0, "P2": 4, "P3": 0}
    assert _observations(chart_data(report), "P2 review findings")[1]["value"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "unknown"),
        ("criteria_sha256", None),
        ("criteria_sha256", "not-a-hash"),
        ("stage_id", "not-a-stage"),
        ("requested_model", "another-judge"),
        ("requested_reasoning_effort", "low"),
        ("finding_count", None),
        ("finding_count", True),
        ("finding_count", 1),
        ("issues", None),
    ],
)
def test_invalid_fixed_review_does_not_turn_into_zero(field: str, value: object) -> None:
    report = _fixed_review_report()
    _fixed_review(report)[field] = value
    row = _observations(chart_data(report), "Total review findings")[1]
    assert row["value"] is None and row["status"] == "unknown"
    assert row["compatibility"] is None


def test_different_fixed_review_criteria_have_no_comparison_bars() -> None:
    report = _fixed_review_report()
    _fixed_review(report)["criteria_sha256"] = "sha256:" + "b" * 64
    data = chart_data(report)
    rows = _observations(data, "Total review findings")
    assert rows[0]["compatibility"] != rows[1]["compatibility"]
    assert rows[1]["value"] == 0 and rows[1]["status"] == "not comparable"
    assert not _matched(data, "Total review findings")


def test_finding_drilldown_escapes_source_and_preserves_review_fields() -> None:
    report = _fixed_review_report()
    issue = {
        "issue_id": "issue-1",
        "category": "correctness",
        "severity": "P1",
        "description": "Untrusted <description>",
        "rationale": "Impact explanation",
        "path": "src/fixture.ts",
        "line": 1,
        "evidence": "</script><script>alert(1)</script>",
        "smallest_fix": "Small correction",
    }
    _fixed_review(report)["issues"] = [issue]
    _fixed_review(report)["finding_count"] = 1
    html, _ = chart_artifacts_from_data(chart_data(report))
    assert "</script><script>alert" not in html
    payload = re.search(
        r'<script id="assessment-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert payload is not None
    data = json.loads(payload.group(1))
    assert data["review_findings"][1]["issues"] == [issue]
    controller = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "innerHTML" not in controller
    assert 'create("pre", issue.evidence, attempt)' in controller
    assert 'issue.path + ":" + issue.line' in controller
    assert "issue.rationale" in controller and "issue.smallest_fix" in controller


def test_review_count_hash_prefix_is_display_only() -> None:
    report = _fixed_review_report()
    _fixed_review(report)["criteria_sha256"] = "a" * 64
    rows = _observations(chart_data(report), "Total review findings")
    assert rows[0]["compatibility"] == rows[1]["compatibility"]


def test_missing_review_attempt_remains_in_findings_and_metric_denominators() -> None:
    report = _fixed_review_report()
    _attempt(report).pop("independent_review")
    data = chart_data(report)
    rows = _observations(data, "Total review findings")
    assert len(rows) == 2 and rows[1]["value"] is None
    findings = _chart_rows(data, "review_findings")
    assert len(findings) == 2 and findings[1]["status"] == "unknown"
    assert findings[1]["issues"] is None


@pytest.mark.parametrize(
    "baseline_criteria,candidate_criteria,comparable",
    [
        ("sha256:" + "a" * 64, "a" * 64, True),
        ("a" * 64, "b" * 64, False),
        (None, None, False),
        (None, "a" * 64, False),
    ],
)
def test_review_token_comparisons_require_same_bound_criteria(
    baseline_criteria: str | None, candidate_criteria: str | None, comparable: bool
) -> None:
    report = _fixed_review_report()
    for index, criteria in enumerate((baseline_criteria, candidate_criteria)):
        review = _fixed_review(report, index)
        review["criteria_sha256"] = criteria
        review["work"] = {
            "tokens": {"input_tokens": 90, "output_tokens": 15},
            "token_format": "native codex",
        }
    data = chart_data(report)
    for metric, value in (("Judge input tokens", 90), ("Judge output tokens", 15)):
        rows = _observations(data, metric)
        assert [row["value"] for row in rows] == [value, value]
        assert bool(_matched(data, metric)) is comparable
        assert (
            bool(rows[0]["compatibility"] and rows[0]["compatibility"] == rows[1]["compatibility"])
            is comparable
        )


@pytest.mark.parametrize("statement_support", ["measured", "unsupported", "empty"])
def test_coverage_observations_keep_each_units_supporting_counts(statement_support: str) -> None:
    report = _report()
    coverage = _metric_values(report, "coverage")
    coverage.update(
        line_total=10,
        line_covered=8,
        line_percent=80.0,
        line_denominator_digest="lines",
        line_population_complete=True,
        statement_total=7,
        statement_covered=3,
        statement_percent=300 / 7,
        statement_denominator_digest="statements",
        statement_population_complete=True,
    )
    if statement_support != "measured":
        coverage.update(
            format="lcov",
            statement_total=0 if statement_support == "empty" else None,
            statement_covered=0 if statement_support == "empty" else None,
            statement_percent=None,
            statement_denominator_digest="empty" if statement_support == "empty" else None,
            statement_population_complete=statement_support == "empty",
        )
    rows = _chart_rows(chart_data(report), "observations")
    for metric, covered, total in (("Line coverage %", 8, 10), ("Statement coverage %", 3, 7)):
        row = next(
            row
            for row in rows
            if row["metric"] == metric and row["arm"] == "candidate" and row["role"] == "handoff"
        )
        provenance = row["provenance"]
        assert isinstance(provenance, dict)
        if metric == "Statement coverage %" and statement_support != "measured":
            count = 0 if statement_support == "empty" else None
            assert row["value"] is None
            assert row["compatibility"] is None
            assert provenance["covered"] == provenance["total"] == count
            expected = (
                "measured statement population is empty; percentage is undefined"
                if statement_support == "empty"
                else "statement coverage is not supplied by lcov"
            )
            assert row["reason"] == expected + "; unknown is not zero"
        else:
            assert provenance["covered"] == covered
            assert provenance["total"] == total
