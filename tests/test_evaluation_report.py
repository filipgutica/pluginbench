from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
from pathlib import Path
import subprocess

import pytest

from pluginbench.evaluation_report import evaluation_data, write_evaluation_report
from pluginbench.assessment_charts import chart_data
from test_assessment_charts import (
    _attempt,
    _empty_baseline_population_report,
    _fixed_review_report,
    _report,
    _stage,
)


def _config(*measurements: str) -> dict[str, object]:
    return {
        "runner": {"harness": "codex", "model": "exact-model", "reasoning": "medium"},
        "measurements": list(measurements or ("tokens",)),
        "comparison_identity": {"task_package": "frozen-digest", "runner": "bound-model"},
    }


def _rows(data: dict[str, object], key: str) -> list[dict[str, object]]:
    rows = data[key]
    assert isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
    return rows


def _token_report() -> dict[str, object]:
    report = _report()
    attempts = []
    for arm, values in {
        "baseline": (80, 100, 120),
        "workflow": (60, 70, 80),
        "superpowers": (110, 130, 150),
    }.items():
        for repeat, value in enumerate(values):
            attempt = copy.deepcopy(_attempt(report, 0))
            attempt.update(id=f"{arm}-{repeat}", arm=arm)
            stages = attempt["stages"]
            assert isinstance(stages, list) and isinstance(stages[0], dict)
            work = stages[0]["work"]
            assert isinstance(work, dict)
            work["tokens"] = {"input_tokens": value, "output_tokens": 10}
            stages[0]["execution_provenance"] = {
                "artifact": {"path": f"{arm}-{repeat}/execution.json", "sha256": "hash"},
                "huge_receipt": "x" * 100_000,
            }
            attempts.append(attempt)
    report["attempts"] = attempts
    return report


@pytest.mark.parametrize("target", ["directory", "results.json", "results.csv", "report.html"])
def test_public_report_rejects_symlinks_before_any_write(tmp_path: Path, target: str) -> None:
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("untouched")
    output = tmp_path / "report"
    if target == "directory":
        output.symlink_to(external, target_is_directory=True)
    else:
        output.mkdir()
        (output / target).symlink_to(sentinel)
    before = {path: path.read_bytes() for path in external.iterdir()}
    with pytest.raises(ValueError, match="symlinks"):
        write_evaluation_report(_token_report(), _config(), output)
    assert {path: path.read_bytes() for path in external.iterdir()} == before
    if target != "directory":
        assert sorted(path.name for path in output.iterdir()) == [target]


def test_each_guidance_set_has_its_own_delta_against_the_same_evaluation_baseline() -> None:
    data = evaluation_data(_token_report(), _config())
    comparisons = {
        row["guidance"]: row
        for row in _rows(data, "comparisons")
        if row["metric"] == "Input tokens"
    }
    assert comparisons["workflow"]["baseline_median"] == 100
    assert comparisons["workflow"]["guidance_median"] == 70
    assert comparisons["workflow"]["absolute_delta_vs_vanilla"] == -30
    assert comparisons["workflow"]["percentage_delta_vs_vanilla"] == -30
    assert comparisons["superpowers"]["baseline_median"] == 100
    assert comparisons["superpowers"]["guidance_median"] == 130
    assert comparisons["superpowers"]["absolute_delta_vs_vanilla"] == 30
    assert all(
        row["baseline_assigned"] == row["baseline_measured"] == 3 for row in comparisons.values()
    )
    raw = [
        row["value"]
        for row in _rows(data, "observations")
        if row["arm"] == "baseline" and row["metric"] == "Input tokens"
    ]
    assert raw == [80, 100, 120]
    assert data["comparison_identity"] == _config()["comparison_identity"]
    assert "winner" not in data and "score" not in data


def test_reused_baseline_is_disclosed_without_recounting_original_calls() -> None:
    report = _token_report()
    report["baseline_origin"] = {
        "baseline-0": {"cache_key": "key", "origin_run": "earlier", "origin_attempt": "old"}
    }
    report["study_progress"] = {"native_invocations": 8}
    data = evaluation_data(report, _config())
    assert data["coding_invocations"] == 8
    assert data["reused_baseline_attempts"] == 1
    limitations = data["limitations"]
    assert isinstance(limitations, list)
    assert "reused" in " ".join(str(row) for row in limitations)
    row = next(row for row in _rows(data, "attempts") if row["id"] == "baseline-0")
    assert row["baseline_origin"] == {
        "cache_key": "key",
        "origin_run": "earlier",
        "origin_attempt": "old",
        "evidence": None,
    }


def test_report_records_jobs_without_changing_attempts_or_metric_values() -> None:
    config = _config()
    serial = evaluation_data(_token_report(), config)
    parallel = evaluation_data(_token_report(), {**config, "jobs": 4})
    assert serial["jobs"] == 1 and parallel["jobs"] == 4
    assert serial["attempts"] == parallel["attempts"]
    assert serial["observations"] == parallel["observations"]


@pytest.mark.parametrize("coding_collectors", [True, False])
def test_completeness_only_requires_selected_tokens_for_every_assigned_attempt(
    coding_collectors: bool,
) -> None:
    report = _token_report()
    if not coding_collectors:
        for attempt in _rows(report, "attempts"):
            stages = _rows(attempt, "stages")
            metrics = stages[0]["metrics"]
            assert isinstance(metrics, dict)
            metrics.clear()
    original = chart_data(report)["measurement_completeness"]
    data = evaluation_data(report, _config("tokens"))
    rows = _rows(data, "measurement_completeness")
    assert len(rows) == 9
    assert all(row["counts"] == {"Input/output tokens": True} and row["complete"] for row in rows)
    assert chart_data(report)["measurement_completeness"] == original


def test_selected_review_completeness_keeps_missing_review_and_cost_unknown() -> None:
    missing = _rows(evaluation_data(_report(), _config("review")), "measurement_completeness")
    assert all(
        row["counts"] == {"Review findings": False, "Review input/output tokens": False}
        and not row["complete"]
        for row in missing
    )
    reviewed = _rows(
        evaluation_data(_fixed_review_report(), _config("review")), "measurement_completeness"
    )
    assert all(
        row["counts"] == {"Review findings": True, "Review input/output tokens": False}
        and not row["complete"]
        for row in reviewed
    )


def _complete_selected_population_report() -> dict[str, object]:
    report = _empty_baseline_population_report()
    for index in (0, 1):
        comparison = _stage(report, index)["metric_comparison"]
        assert isinstance(comparison, dict)
        comparison["fallow"]["changed_functions"] = {
            "status": "available",
            "method": "bound source hunk overlap",
            "after": {
                "function_count_reported": 0,
                "max_cognitive_reported": None,
                "max_cyclomatic_reported": None,
                "complete_for_declared_scope": True,
            },
        }
    return report


def test_selected_complexity_completeness_keeps_complete_empty_populations() -> None:
    report = _complete_selected_population_report()
    data = evaluation_data(report, _config("cognitive", "cyclomatic"))
    assert all(
        row["counts"] == {"Complexity population": True} and row["complete"]
        for row in _rows(data, "measurement_completeness")
    )
    baseline = next(
        row
        for row in _rows(data, "observations")
        if row["arm"] == "baseline" and row["metric"] == "New-file function cognitive maximum"
    )
    provenance = baseline["provenance"]
    assert isinstance(provenance, dict)
    assert baseline["value"] is None and provenance["reported_functions"] == 0


@pytest.mark.parametrize("fault", ["incomplete_empty", "unknown_count", "undefined_nonempty"])
def test_selected_complexity_requires_declared_complete_populations_and_defined_nonempty_maxima(
    fault: str,
) -> None:
    report = _complete_selected_population_report()
    comparison = _stage(report, 1)["metric_comparison"]
    assert isinstance(comparison, dict)
    if fault == "incomplete_empty":
        comparison["fallow"]["changed_functions"]["after"]["complete_for_declared_scope"] = False
    else:
        population = comparison["fallow"]["new_file_functions"]["after"]
        population[
            "function_count_reported" if fault == "unknown_count" else "max_cognitive_reported"
        ] = None
    rows = _rows(evaluation_data(report, _config("cognitive")), "measurement_completeness")
    assert rows[0]["counts"] == {"Complexity population": True} and rows[0]["complete"]
    assert rows[1]["counts"] == {"Complexity population": False} and not rows[1]["complete"]


@pytest.mark.parametrize("family", ["cognitive", "cyclomatic"])
def test_complete_all_function_scan_does_not_capture_missing_selected_changed_populations(
    family: str,
) -> None:
    report = _empty_baseline_population_report()
    for index in (0, 1):
        metrics = _stage(report, index)["metrics"]
        assert isinstance(metrics, dict)
        metrics["fallow"]["metrics"]["function_count_reported"] = 3
    measured = chart_data(report)["measurement_completeness"]
    assert isinstance(measured, list)
    assert all(row["counts"]["Complexity population"] for row in measured)
    data = evaluation_data(report, _config(family))
    assert all(
        row["counts"] == {"Complexity population": False} and not row["complete"]
        for row in _rows(data, "measurement_completeness")
    )
    assert chart_data(report)["measurement_completeness"] == measured


def test_selected_token_completeness_preserves_missing_output_counts() -> None:
    report = _token_report()
    work = _stage(report, 4)["work"]
    assert isinstance(work, dict)
    work["tokens"] = {"input_tokens": 70}
    rows = _rows(evaluation_data(report, _config("tokens")), "measurement_completeness")
    assert len(rows) == 9 and sum(bool(row["complete"]) for row in rows) == 8
    missing = next(row for row in rows if row["attempt_id"] == "workflow-1")
    assert missing["counts"] == {"Input/output tokens": False}


def test_selected_coverage_requires_collected_denominators_not_just_numeric_rates() -> None:
    data = evaluation_data(_report(), _config("coverage"))
    assert all(
        row["value"] is not None
        for row in _rows(data, "observations")
        if row["metric"] in {"Branch coverage %", "Function coverage %"}
    )
    assert all(
        row["counts"] == {"Coverage covered/total counts": False} and not row["complete"]
        for row in _rows(data, "measurement_completeness")
    )


def _evaluation_plot_rows(
    data: dict[str, object], tmp_path: Path, *, metric: str, view: str = "raw", task: str = ""
) -> list[dict[str, object]]:
    controller = Path("src/pluginbench/assessment-charts.html").read_text().rsplit("<script>", 1)[1]
    function = re.search(r"function evaluationPlotRows\(\{.*?\n\}", controller, re.S)
    assert function is not None
    settings = {"view": view, "task": task, "metric": metric}
    script = tmp_path / "evaluation-plot-rows.js"
    script.write_text(
        "const data="
        + json.dumps(data)
        + "; const settings="
        + json.dumps(settings)
        + ";\n"
        + "const comparisonView={value:settings.view}, chartBreakdown={value:'tasks'}, taskSelect={value:settings.task};\n"
        + "const unique=items=>[...new Set(items)]; const armLabel=arm=>arm==='baseline'?'Vanilla':arm;\n"
        + function.group(0)
        + "\nconsole.log(JSON.stringify(evaluationPlotRows({rows:data.observations.filter(row=>row.metric===settings.metric),metric:settings.metric})));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return _rows({"rows": json.loads(result.stdout)}, "rows")


def test_actual_task_plot_rows_have_raw_status_and_reason_and_baseline_first(
    tmp_path: Path,
) -> None:
    data = evaluation_data(_unequal_repeat_report(), _config("tokens"))
    rows = _evaluation_plot_rows(data, tmp_path, metric="Input tokens")
    assert [(row["task"], row["guidance"]) for row in rows] == [
        (task, arm)
        for task in ("fixture-task", "second-task")
        for arm in ("baseline", "superpowers", "workflow")
    ]
    assert [row["plotted"] for row in rows] == [100, 130, 70, 300, 200, 400]
    assert all(
        row["status"] == "measured" and isinstance(row["reason"], str) and row["reason"]
        for row in rows
    )
    complexity = evaluation_data(_empty_baseline_population_report(), _config("cognitive"))
    metric = "New-file function cognitive maximum"
    assert [
        row["plotted"] for row in _evaluation_plot_rows(complexity, tmp_path, metric=metric)
    ] == [14]
    assert [
        row["plotted"]
        for row in _evaluation_plot_rows(complexity, tmp_path, metric=metric, view="delta")
    ] == [None]


def test_actual_raw_task_plot_preserves_unknown_profile_attempt_membership(tmp_path: Path) -> None:
    report = _report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    repeat = copy.deepcopy(_attempt(report, 1))
    repeat["id"] = "candidate-repeat"
    attempts.append(repeat)
    for index, value in ((1, 100), (2, 200)):
        provenance = _attempt(report, index)["provenance"]
        work = _stage(report, index)["work"]
        assert isinstance(provenance, dict) and isinstance(work, dict)
        provenance["reasoning_effort"] = "unknown"
        work["tokens"] = {"input_tokens": value, "output_tokens": 10}
    data = evaluation_data(report, _config("tokens"))
    rows = [
        row
        for row in _evaluation_plot_rows(data, tmp_path, metric="Input tokens", task="fixture-task")
        if row["guidance"] == "candidate"
    ]
    assert [row["plotted"] for row in rows] == [100, 200]
    assert [[run["attempt_id"] for run in _rows(row, "rawRuns")] for row in rows] == [
        ["candidate"],
        ["candidate-repeat"],
    ]


def test_zero_vanilla_keeps_absolute_delta_but_percentage_unknown() -> None:
    report = _report()
    for index, value in ((0, 0), (1, 5)):
        work = _stage(report, index)["work"]
        assert isinstance(work, dict)
        work["tokens"] = {"input_tokens": value}
    data = evaluation_data(report, _config())
    comparison = next(row for row in _rows(data, "comparisons") if row["metric"] == "Input tokens")
    assert comparison["absolute_delta_vs_vanilla"] == 5
    assert comparison["percentage_delta_vs_vanilla"] is None
    assert comparison["status"] == "complete"
    missing = next(row for row in _rows(data, "comparisons") if row["metric"] == "Output tokens")
    assert missing["absolute_delta_vs_vanilla"] is None
    assert missing["baseline_assigned"] == missing["guidance_assigned"] == 1
    assert missing["status"] == "unknown"


def test_unpaired_guidance_complexity_stays_visible_in_raw_task_and_overview_values() -> None:
    data = evaluation_data(_empty_baseline_population_report(), _config("cognitive", "cyclomatic"))
    for metric, value in (
        ("New-file function cognitive maximum", 14),
        ("New-file function cyclomatic maximum", 11),
    ):
        overview = next(
            row
            for row in _rows(data, "aggregates")
            if row["metric"] == metric and row["guidance"] == "candidate"
        )
        assert overview["value"] == value and overview["measured_tasks"] == 1
        raw = [row for row in _rows(data, "raw_medians") if row["metric"] == metric]
        assert len(raw) == 1 and raw[0]["value"] == value
        assert overview["status"] == "complete" and overview["comparison_status"] == "unknown"
        assert overview["absolute_delta_vs_vanilla"] is None
        comparison = next(row for row in _rows(data, "comparisons") if row["metric"] == metric)
        assert comparison["absolute_delta_vs_vanilla"] is None
        baseline = next(
            row
            for row in _rows(data, "aggregates")
            if row["metric"] == metric and row["guidance"] == "baseline"
        )
        assert baseline["value"] is None and baseline["measured_tasks"] == 0
    assert data["measurement_completeness"]


def test_overview_does_not_average_incompatible_raw_repeat_profiles() -> None:
    report = _empty_baseline_population_report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    repeat = copy.deepcopy(_attempt(report, 1))
    repeat["id"] = "candidate-repeat"
    attempts.append(repeat)
    metrics = _stage(report, 2)["metrics"]
    assert isinstance(metrics, dict)
    metrics["fallow"]["provenance"]["tool_version"] = "different"
    data = evaluation_data(report, _config("cognitive"))
    metric = "New-file function cognitive maximum"
    raw = [row for row in _rows(data, "raw_medians") if row["metric"] == metric]
    assert len(raw) == 2 and all(row["value"] == 14 for row in raw)
    overview = next(
        row
        for row in _rows(data, "aggregates")
        if row["metric"] == metric and row["guidance"] == "candidate"
    )
    assert overview["value"] is None and overview["measured_tasks"] == 0
    assert overview["absolute_delta_vs_vanilla"] is None


@pytest.mark.parametrize(
    "provenance_state",
    ["missing_repeat", "unknown", "mixed_unknown", "unknown_version", "conflicting"],
)
def test_overview_resolves_analyzers_from_unambiguous_task_peers_only(
    provenance_state: str,
) -> None:
    report = _empty_baseline_population_report()
    attempts = _rows(report, "attempts")
    for index in (0, 1):
        metrics = _stage(report, index)["metrics"]
        assert isinstance(metrics, dict)
        metrics["fallow"]["provenance"] = (
            {}
            if provenance_state == "unknown" or (index and provenance_state == "mixed_unknown")
            else {
                "language": "python",
                "analyzers": {
                    "rca": {
                        "version": "unknown"
                        if provenance_state == "unknown_version"
                        else "RCA 0.0.26"
                        if index and provenance_state == "conflicting"
                        else "RCA 0.0.25"
                    }
                },
            }
        )
    missing = copy.deepcopy(_attempt(report, 1))
    missing["id"] = "missing-repeat"
    missing_stage = _rows(missing, "stages")[0]
    missing_stage["metrics"] = {}
    missing_stage["metric_comparison"] = {}
    attempts.append(missing)

    data = evaluation_data(report, _config("cognitive"))
    metric = "New-file function cognitive maximum"
    overview = [
        row
        for row in _rows(data, "aggregates")
        if row["metric"] == metric and row["guidance"] == "candidate"
    ]
    assert len(overview) == 1
    expected_group = {
        "missing_repeat": "Python · RCA 0.0.25",
        "unknown": "Unknown analyzer",
        "mixed_unknown": "Unknown analyzer",
        "unknown_version": "Unknown analyzer",
        "conflicting": "Conflicting analyzers",
    }[provenance_state]
    assert overview[0]["analysis_group"] == expected_group
    assert overview[0]["value"] == (14 if provenance_state == "missing_repeat" else None)
    assert overview[0]["status"] == (
        "partial" if provenance_state == "missing_repeat" else "unknown"
    )
    assert overview[0]["absolute_delta_vs_vanilla"] is None
    assert data["assigned"] == 3 and overview[0]["assigned_tasks"] == 1
    observations = [row for row in _rows(data, "observations") if row["metric"] == metric]
    assert {row["attempt_id"]: row["value"] for row in observations} == {
        "baseline": None,
        "candidate": 14,
        "missing-repeat": None,
    }
    raw = [row for row in _rows(data, "raw_medians") if row["metric"] == metric]
    assert len(raw) == 1 and raw[0]["value"] == 14


def test_partial_measurements_keep_assigned_counts_and_unknown_raw_observations() -> None:
    report = _token_report()
    attempt = _attempt(report, 4)
    stages = attempt["stages"]
    assert isinstance(stages, list) and isinstance(stages[0], dict)
    stages[0]["work"] = {"tokens": {"output_tokens": 10}, "token_format": "native codex"}
    data = evaluation_data(report, _config())
    comparisons = [
        row
        for row in _rows(data, "comparisons")
        if row["guidance"] == "workflow" and row["metric"] == "Input tokens"
    ]
    partial = next(row for row in comparisons if row["status"] == "partial")
    assert partial["guidance_assigned"] == 3 and partial["guidance_measured"] == 2
    unknown = next(
        row
        for row in _rows(data, "observations")
        if row["attempt_id"] == "workflow-1" and row["metric"] == "Input tokens"
    )
    assert unknown["value"] is None and unknown["status"] == "unknown"


@pytest.mark.parametrize("mismatch", ["criteria", "model"])
def test_incompatible_runtime_or_review_criteria_has_no_delta(mismatch: str) -> None:
    report = _fixed_review_report()
    candidate = _attempt(report)
    if mismatch == "criteria":
        review = candidate["independent_review"]
        assert isinstance(review, dict)
        review["criteria_sha256"] = "b" * 64
        config = _config("review")
        metric = "Total review findings"
    else:
        candidate["model"] = "different-model"
        config = _config("tokens")
        metric = "Input tokens"
    data = evaluation_data(report, config)
    comparison = next(row for row in _rows(data, "comparisons") if row["metric"] == metric)
    assert comparison["absolute_delta_vs_vanilla"] is None and comparison["status"] == "unknown"
    raw = next(
        row
        for row in _rows(data, "observations")
        if row["arm"] == "candidate" and row["metric"] == metric
    )
    assert raw["value"] is not None and raw["status"] == "not comparable"


def test_only_selected_families_and_compact_runtime_evidence_are_exported() -> None:
    report = _token_report()
    original = copy.deepcopy(report)
    data = evaluation_data(report, _config("tokens", "lines"))
    assert {row["family"] for row in _rows(data, "observations")} == {"tokens", "lines"}
    assert data["independent_review"] is None and data["review_findings"] == []
    assert "outcomes" not in data and "accepted" not in data
    assert "huge_receipt" not in json.dumps(data)
    attempt = _rows(data, "attempts")[0]
    artifacts = attempt["artifacts"]
    assert isinstance(artifacts, dict) and artifacts["execution"] == {
        "path": "baseline-0/execution.json",
        "sha256": "hash",
    }
    assert report == original


def _shared_helpers(controller: str) -> str:
    """Return the template's own ordering and formatting helpers for extracted functions."""
    names = (
        "formatCompact",
        "formatSigned",
        "packageNames",
        "armRank",
        "byArm",
        "seriesVariable",
        "formatLimited",
        "KEY_METRICS",
    )
    lines = [re.search(rf"^const {name} = .*$", controller, re.M) for name in names]
    assert all(lines)
    return "".join(line.group(0) + "\n" for line in lines if line)


def test_summary_numbers_keep_small_nonzero_differences(tmp_path: Path) -> None:
    controller = Path("src/pluginbench/assessment-charts.html").read_text().rsplit("<script>", 1)[1]
    script = tmp_path / "format.js"
    script.write_text(
        "const data={attempts:[]},observations=[],unique=items=>[...new Set(items)];\n"
        + _shared_helpers(controller)
        + "console.log(JSON.stringify([0.014,-0.004,0,2133.5,1281072.25].map(formatSigned)));\n"
    )
    # The template formats with the reader's locale; pin one for a deterministic expectation.
    locale = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, env=locale)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["+0.014", "-0.004", "0", "+2,133.5", "+1.28M"]


def _outcome_text(
    data: dict[str, object], tmp_path: Path, *, package: bool = False, task: str = ""
) -> list[str]:
    controller = Path("src/pluginbench/assessment-charts.html").read_text().rsplit("<script>", 1)[1]
    name = "showPackageSummary" if package else "showOutcomes"
    function = re.search(r"function " + name + r"\(\) \{.*?\n\}", controller, re.S)
    assert function is not None
    summary = re.search(r"function taskCheckSummary\(attempts\) \{.*?\n\}", controller, re.S)
    script = tmp_path / "outcomes.js"
    script.write_text(
        "const data=" + json.dumps(data) + ";\n"
        "const texts=[];const element=()=>({replaceChildren(){},querySelector(){return null},setAttribute(){},append(){},prepend(){},style:{setProperty(){}},cloneNode(){return element()},addEventListener(){},firstChild:{}});\n"
        "const document={getElementById:element},packageSummary=element(),taskSelect={value:"
        + json.dumps(task)
        + "},observations=data.observations,metricSelect={value:''};\n"
        "const unique=items=>[...new Set(items)],armLabel=arm=>arm==='baseline'?'Vanilla':arm;\n"
        "const emptyFunctionMaximum=()=>false,format=value=>String(value);\n"
        "const create=(tag,text,parent)=>{if(text!==undefined)texts.push(String(text));return element();};\n"
        + _shared_helpers(controller)
        + (summary.group(0) if summary else "")
        + function.group(0)
        + "\n"
        + name
        + "();console.log(JSON.stringify(texts));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    texts = json.loads(result.stdout)
    assert isinstance(texts, list) and all(isinstance(text, str) for text in texts)
    return texts


@pytest.mark.parametrize("execution_state", ["failed", "timed_out"])
def test_writer_headlines_separate_task_check_failures_from_execution_failures(
    tmp_path: Path,
    execution_state: str,
) -> None:
    report = _token_report()
    attempts: list[dict[str, object]] = []
    for index, (correctness, execution, task) in enumerate(
        (
            ("pass", "complete", "fixture-task"),
            ("fail", "complete", "fixture-task"),
            ("fail", execution_state, "fixture-task"),
            ("unknown", "complete", "fixture-task"),
            ("pass", "complete", "second-task"),
        )
    ):
        attempt = copy.deepcopy(_attempt(report, 0))
        attempt.update(id=f"run-{index}", task_id=task)
        stages = attempt["stages"]
        assert isinstance(stages, list) and isinstance(stages[0], dict)
        stage = stages[0]
        stage["correctness"] = {"status": correctness}
        provenance = stage["execution_provenance"]
        assert isinstance(provenance, dict)
        provenance.update(
            successful_native_completion=execution == "complete",
            timed_out=execution == "timed_out",
            exit_code=0 if execution == "complete" else 1,
        )
        attempts.append(attempt)
    report["attempts"] = attempts
    original = copy.deepcopy(report)
    result = write_evaluation_report(report, _config("tokens"), tmp_path)
    data = json.loads(result.read_text())
    assert data["assigned"] == 5
    assert [row["correctness"] for row in data["attempts"]] == [
        "pass",
        "fail",
        "fail",
        "unknown",
        "pass",
    ]
    assert data["attempts"][2]["execution"]["status"] == execution_state
    for package in (False, True):
        for task in ("", "fixture-task"):
            visible = _outcome_text(data, tmp_path, package=package, task=task)
            assert "1 failed task check · 1 execution failure · 1 unknown" in visible
            assert (
                "2/5 attempts passed" if package and not task else "1/4 attempts passed"
            ) in visible
    assert report == original


@pytest.mark.parametrize("execution_state", ["provider", "timeout", "complete", "missing"])
@pytest.mark.parametrize("measurement", ["tokens", "lines"])
def test_writer_json_csv_and_echarts_share_exact_selected_values(
    tmp_path: Path,
    execution_state: str,
    measurement: str,
) -> None:
    report = _token_report()
    stage = _stage(report, 0)
    message = "Selected model is at capacity. <provider detail>"
    partial_usage = [
        {
            "status": "incomplete usage lower bound",
            "tokens": {"input_tokens": 40, "output_tokens": 3},
        }
    ]
    if execution_state != "missing":
        execution = stage["execution_provenance"]
        assert isinstance(execution, dict)
        execution.update(
            successful_native_completion=execution_state == "complete",
            timed_out=execution_state == "timeout",
            exit_code=0 if execution_state == "complete" else 1,
            stream_errors=[{"type": "turn.failed", "error": {"message": message}}]
            if execution_state == "provider"
            else [],
            error={"exception_type": "ApiOverloadedError"}
            if execution_state == "provider"
            else None,
            partial_usage=partial_usage if execution_state != "complete" else [],
        )
        stage["correctness"] = {"status": "fail"}
        if execution_state != "complete":
            work = stage["work"]
            assert isinstance(work, dict)
            work["tokens"] = None
    result = write_evaluation_report(report, _config(measurement), tmp_path)
    data = json.loads(result.read_text())
    html = (tmp_path / "report.html").read_text()
    payload = re.search(
        r'<script id="assessment-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert payload is not None and json.loads(payload.group(1)) == data
    csv_rows = list(csv.DictReader(io.StringIO((tmp_path / "results.csv").read_text())))
    assert len(csv_rows) == len(data["observations"])
    assert [float(row["value"]) if row["value"] else None for row in csv_rows] == [
        row["value"] for row in data["observations"]
    ]
    assert {row["family"] for row in csv_rows} == {measurement}
    assert "data.download_csv" in html and "Delta vs vanilla" in html
    attempt = data["attempts"][0]
    execution = attempt["execution"]
    expected_status = {
        "provider": "failed",
        "timeout": "timed_out",
        "complete": "complete",
        "missing": "unknown",
    }[execution_state]
    assert execution["status"] == expected_status
    visible = _outcome_text(data, tmp_path)
    selected_rows = [row for row in csv_rows if row["attempt_id"] == "baseline-0"]
    for row in selected_rows:
        provenance = json.loads(row["provenance"])
        assert provenance["execution_status"] == expected_status
        assert provenance["execution_reason"] == execution["reason"]
    if execution_state in {"provider", "timeout"}:
        reason = execution["reason"]
        assert reason in visible
        assert message in reason if execution_state == "provider" else "timed out" in reason
        assert execution["partial_usage"] == partial_usage
        assert measurement != "tokens" or all(
            not row["value"]
            and row["status"] == "unknown"
            and reason in row["reason"]
            and "lower bounds" in row["reason"]
            for row in selected_rows
        )
    else:
        assert execution["reason"] is None
        assert "Native execution failures" not in visible
        if measurement == "tokens":
            assert [float(row["value"]) for row in selected_rows] == [80, 10]
        if execution_state == "complete":
            assert attempt["correctness"] == "fail"
    if measurement == "lines":
        assert [float(row["value"]) for row in selected_rows] == [2, 1, 5, 0]
        peers = {row["metric"]: row for row in csv_rows if row["attempt_id"] == "baseline-1"}
        assert all(
            row["status"] == peers[row["metric"]]["status"] == "measured"
            and row["compatibility"] == peers[row["metric"]]["compatibility"]
            and row["compatibility"]
            for row in selected_rows
        )
    controller = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "innerHTML" not in controller and "textContent" in controller


@pytest.mark.parametrize("proof", [True, False])
def test_claude_launch_comparison_keeps_effective_reasoning_unknown(proof: bool) -> None:
    report = _report()
    for index in (0, 1):
        provenance = _attempt(report, index)["provenance"]
        assert isinstance(provenance, dict)
        provenance.update(
            host="claude",
            harness="claude-cli",
            reasoning_effort="unknown",
            requested_reasoning_effort="high",
        )
        if proof:
            provenance.update(
                comparison_reasoning_effort="high",
                launch_harness_version="fixture-1",
                effort_evidence_source="requested launch setting; effective effort unverified",
            )
    data = evaluation_data(report, _config())
    comparison = next(row for row in _rows(data, "comparisons") if row["metric"] == "Input tokens")
    assert (comparison["absolute_delta_vs_vanilla"] == 0) is proof
    assert all(row["reasoning_effort"] == "unknown" for row in _rows(data, "observations"))
    assert all(row["requested_reasoning_effort"] == "high" for row in _rows(data, "observations"))


def _unequal_repeat_report() -> dict[str, object]:
    report = _token_report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    for index, (arm, value) in enumerate(
        (("baseline", 300), ("workflow", 400), ("superpowers", 200))
    ):
        attempt = copy.deepcopy(_attempt(report, 0))
        attempt.update(
            id=f"second-task-{index}", arm=arm, task_id="second-task", before_digest="second-seed"
        )
        stages = attempt["stages"]
        assert isinstance(stages, list) and isinstance(stages[0], dict)
        work = stages[0]["work"]
        assert isinstance(work, dict)
        work["tokens"] = {"input_tokens": value, "output_tokens": 10}
        attempts.append(attempt)
    return report


def test_overview_weights_task_medians_equally_not_repetitions() -> None:
    data = evaluation_data(_unequal_repeat_report(), _config())
    rows = {
        row["guidance"]: row for row in _rows(data, "aggregates") if row["metric"] == "Input tokens"
    }
    assert (
        rows["baseline"]["value"] == 200
    )  # Mean of task medians 100 and 300; not repeat-weighted 150.
    assert rows["workflow"]["value"] == 235  # Task medians 70 and 400.
    assert rows["workflow"]["absolute_delta_vs_vanilla"] == 35
    assert rows["workflow"]["percentage_delta_vs_vanilla"] == 17.5
    assert rows["superpowers"]["value"] == 165
    assert all(
        row["measured_tasks"] == row["assigned_tasks"] == 2 and row["status"] == "complete"
        for row in rows.values()
    )


def test_overview_missing_task_is_partial_and_has_no_unmatched_delta() -> None:
    report = _unequal_repeat_report()
    stages = _attempt(report, 10)["stages"]
    assert isinstance(stages, list) and isinstance(stages[0], dict)
    stages[0]["work"] = {"token_format": "native codex", "tokens": {}}
    data = evaluation_data(report, _config())
    row = next(
        row
        for row in _rows(data, "aggregates")
        if row["metric"] == "Input tokens" and row["guidance"] == "workflow"
    )
    assert row["value"] == 70 and row["status"] == "partial"
    assert row["measured_tasks"] == 1 and row["assigned_tasks"] == 2
    assert row["absolute_delta_vs_vanilla"] is None
    assert row["percentage_delta_vs_vanilla"] is None
    assert row["task_medians"] == [
        {"task": "fixture-task", "value": 70},
        {"task": "second-task", "value": None},
    ]


def test_coverage_absolute_delta_is_percentage_points() -> None:
    report = _report()
    data = evaluation_data(report, _config("coverage"))
    assert all(
        row["absolute_delta_unit"] == "percentage points" for row in _rows(data, "comparisons")
    )
    assert all(
        row["absolute_delta_unit"] == "percentage points" for row in _rows(data, "aggregates")
    )


@pytest.mark.parametrize("bound_launch", [True, False])
def test_claude_reviewer_counts_bind_requested_effort_without_claiming_effective_effort(
    bound_launch: bool,
) -> None:
    report = _fixed_review_report()
    for index in (0, 1):
        review = _attempt(report, index)["independent_review"]
        assert isinstance(review, dict)
        execution = review["execution"]
        assert isinstance(execution, dict)
        execution.update(runtime_effort_evidence=[], startup=[])
        if bound_launch:
            execution["launch"] = {
                "harness": "claude-cli",
                "requested_model": "judge-A",
                "requested_effort": "high",
                "harness_version": "fixture-1",
                "argv": ["claude", "--safe-mode", "--model", "judge-A", "--effort", "high"],
            }
    data = evaluation_data(report, _config("review"))
    comparison = next(
        row for row in _rows(data, "comparisons") if row["metric"] == "Total review findings"
    )
    assert (comparison["absolute_delta_vs_vanilla"] == 0) is bound_launch
    rows = [row for row in _rows(data, "observations") if row["metric"] == "Total review findings"]
    assert all(
        isinstance(row["provenance"], dict) and row["provenance"]["judge_reasoning_effort"] == []
        for row in rows
    )


def test_selected_but_missing_review_stays_assigned_and_unknown() -> None:
    data = evaluation_data(_report(), _config("review"))
    rows = _rows(data, "observations")
    assert len(rows) == 22  # Eleven review metrics for each assigned attempt.
    assert all(row["value"] is None and row["status"] == "unknown" for row in rows)
    assert all(
        row["baseline_assigned"] == row["guidance_assigned"] == 1
        for row in _rows(data, "comparisons")
    )
    assert all(
        row["measured_tasks"] == 0 and row["assigned_tasks"] == 1
        for row in _rows(data, "aggregates")
    )


def test_guidance_package_identity_is_kept_separate_from_comparison_identity() -> None:
    config = _config()
    packages = {
        "workflow": {
            "sha256": "package-digest",
            "files": [{"path": "frozen/guidance/workflow/SKILL.md", "sha256": "file-digest"}],
        }
    }
    config["guidance_packages"] = packages
    data = evaluation_data(_report(), config)
    assert data["guidance_packages"] == packages
    assert data["comparison_identity"] == config["comparison_identity"]


def test_compact_report_retains_artifact_base_for_native_evidence() -> None:
    config = _config()
    config["artifact_base"] = "evidence"
    data = evaluation_data(_token_report(), config)
    assert data["artifact_base"] == "evidence"
    execution = _rows(data, "attempts")[0]["artifacts"]
    assert isinstance(execution, dict) and execution["execution"] == {
        "path": "baseline-0/execution.json",
        "sha256": "hash",
    }


def test_review_harness_mismatch_prevents_delta_even_for_same_custom_model_and_effort() -> None:
    report = _fixed_review_report()
    review = _attempt(report)["independent_review"]
    assert isinstance(review, dict)
    review["requested_harness"] = "claude"
    execution = review["execution"]
    assert isinstance(execution, dict)
    execution["launch"] = {
        "harness": "claude-cli",
        "requested_model": "judge-A",
        "requested_effort": "high",
        "harness_version": "fixture-1",
        "argv": ["claude", "--safe-mode", "--model", "judge-A", "--effort", "high"],
    }
    data = evaluation_data(report, _config("review"))
    comparison = next(
        row for row in _rows(data, "comparisons") if row["metric"] == "Total review findings"
    )
    assert comparison["absolute_delta_vs_vanilla"] is None
    assert comparison["status"] == "unknown"


def test_compact_report_preserves_collection_outcomes_and_repeat_labels() -> None:
    config = _config("tokens")
    receipt = {"path": "observer-revisions/measurements/revision.json", "sha256": "bound"}
    collection = {
        "revision": "measurements",
        "status": "partial",
        "procedure_status": "complete",
        "available_families": 4,
        "unavailable_families": 2,
        "revision_receipt": receipt,
        "history": [],
        "failures": [
            {
                "attempt_id": "baseline-1",
                "family": "coverage",
                "repeat_states": ["available", "unavailable"],
                "receipts": [receipt, receipt],
            }
        ],
    }
    config["measurement_collection"] = collection
    report = _token_report()
    for index, attempt in enumerate(_rows(report, "attempts"), 1):
        attempt["attempt"] = index
    data = evaluation_data(report, config)
    assert data["measurement_collection"] == collection
    assert [attempt["repeat"] for attempt in _rows(data, "attempts")] == list(
        range(1, len(_rows(report, "attempts")) + 1)
    )


def test_report_explains_unsupported_metrics_and_preserves_native_grade_binding() -> None:
    report = _token_report()
    grade = {
        "pass": None,
        "native_verifier_result": {"rewards": {"reward": 1}},
        "binding": "unknown",
        "reason": "native graded patch and measured working tree differ",
    }
    _stage(report, 0)["task_result"] = grade
    config = _config("tokens", "coverage")
    capabilities = {
        "fixture-task": {
            "coverage": {"status": "unavailable", "reason": "No coverage command configured"}
        }
    }
    config["measurement_capabilities"] = capabilities
    # Missing scan evidence remains unknown even when a native reward says pass.
    for attempt in _rows(report, "attempts"):
        for stage in _rows(attempt, "stages"):
            metrics = stage["metrics"]
            assert isinstance(metrics, dict)
            metrics.pop("coverage", None)
    data = evaluation_data(report, config)
    assert data["measurement_capabilities"] == capabilities
    assert _rows(data, "attempts")[0]["native_grading"] == grade
    missing = [row for row in _rows(data, "observations") if row["family"] == "coverage"]
    assert missing and all(
        row["value"] is None
        and row["status"] == "unavailable"
        and row["reason"] == "No coverage command configured"
        for row in missing
    )
    assert all(
        row["value"] is not None for row in _rows(data, "observations") if row["family"] == "tokens"
    )


def test_overview_keeps_language_analyzers_separate() -> None:
    data = {
        "attempts": [{"task": "py"}, {"task": "go"}],
        "observations": [
            {
                "task": "py",
                "metric": "After changed-function cognitive maximum",
                "family": "cognitive",
                "provenance": {
                    "language": "python",
                    "analyzers": {
                        "rca": {"status": "available", "version": "rust-code-analysis-cli 0.0.25"}
                    },
                },
            },
            {
                "task": "go",
                "metric": "After changed-function cognitive maximum",
                "family": "cognitive",
                "provenance": {
                    "language": "go",
                    "analyzers": {
                        "go_analysis": {
                            "status": "available",
                            "version": "go-analysis-v1 gocognit=1.3.0 gocyclo=0.6.0",
                        }
                    },
                },
            },
        ],
    }
    from pluginbench.evaluation_report import _aggregate_groups

    groups = _aggregate_groups(data)
    assert len(groups) == 2
    assert {tuple(group[2]) for group in groups} == {("py",), ("go",)}
    assert any("gocognit 1.3.0" in str(group[1]) for group in groups)


def test_rust_unused_aggregate_names_rustc_not_complexity_analyzer() -> None:
    from pluginbench.evaluation_report import _aggregate_groups

    data = {
        "attempts": [{"task": "rust"}],
        "observations": [
            {
                "task": "rust",
                "metric": "Introduced dead findings",
                "family": "dead_code",
                "provenance": {
                    "language": "rust",
                    "analyzers": {
                        "rca": {"version": "RCA 0.0.25"},
                        "rustc": {"version": "rustc 1.92.0"},
                    },
                },
            }
        ],
    }
    groups = _aggregate_groups(data)
    assert groups[0][1] == "Rust · rustc 1.92.0"
