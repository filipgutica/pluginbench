from __future__ import annotations

import copy
import csv
import io
import json
import re
import subprocess
from pathlib import Path

import pytest

from pluginbench.assessment import build_assessment, write_assessment
from pluginbench.assessment_charts import METRICS, chart_artifacts, chart_data


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
        "mutation": {
            "status": "available",
            "provenance": provenance,
            "metrics": {
                "mutant_set_digest": "mutants",
                "per_test_count": 2,
                "zero_unique_mutant_kill_tests": ["one"],
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
            "final": "handoff",
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


def test_final_review_tokens_bind_the_reviewed_submission_and_phase() -> None:
    report = _report()
    report["independent_review"] = {"status": "complete", "stage": "final"}
    for index in (0, 1):
        attempt = _attempt(report, index)
        stages = attempt["stages"]
        assert isinstance(stages, list)
        final = copy.deepcopy(_stage(report, index))
        final.update(
            id="repair-1", snapshot_digest="final-snapshot", correctness={"status": "pass"}
        )
        stages.append(final)
        _stage(report, index)["correctness"] = {"status": "fail"}
        attempt["final"] = "repair-1"
        attempt["independent_review"] = {
            "stage": "final",
            "stage_id": "repair-1",
            "work": {
                "tokens": {"input_tokens": 50},
                "token_format": "native codex",
                "elapsed_seconds": 7,
            },
            "execution": {
                "actual_primary_models": ["judge-A"],
                "runtime_effort_evidence": ["high"],
                "startup": [{"type": "session_meta", "payload": {"cli_version": "fixture-1"}}],
            },
        }
    rows = _observations(chart_data(report), "Judge input tokens")
    assert all(
        row["stage"] == "repair-1"
        and row["snapshot_digest"] == "final-snapshot"
        and row["correctness"] == "pass"
        and row["elapsed_seconds"] == 7
        and isinstance(row["provenance"], dict)
        and row["provenance"]["review_stage"] == "final"
        for row in rows
    )
    review = _attempt(report)["independent_review"]
    assert isinstance(review, dict)
    review["stage"] = "handoff"
    assert not _matched(chart_data(report), "Judge input tokens")


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
    """Run only shipped initialization and the complexity note with native-select stubs."""
    template = Path("src/pluginbench/assessment-charts.html").read_text()
    controller = template.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    initializer = controller.split('document.getElementById("stage-control")', 1)[0]
    note = re.search(r"function complexityNote\(parent, rows\) \{.*?\n\}", controller, re.S)
    assert note is not None
    script = tmp_path / "controller.js"
    stub = r"""
class Select {
  constructor() { this.options = []; }
  append(option) { this.options.push(option); }
  get value() { return this.selection ?? this.options[0]?.value ?? ''; }
  set value(value) { this.selection = value; }
}
const ids = new Map();
const document = {
  getElementById: id => {
    if (!ids.has(id)) ids.set(id, new Select());
    return ids.get(id);
  },
  createElement: () => ({}),
};
"""
    capture = r"""
const visible = initialRows.filter(row => runtimeKey(row) === runtime.value);
const notes = [];
complexityNote({append: node => notes.push(node.textContent)}, visible.filter(row => row.panel === 'Complexity'));
console.log(JSON.stringify({
  task: taskSelect.value,
  runtime: configurations.get(runtime.value),
  visible,
  complexity_note: notes.join(' '),
}));
"""
    script.write_text(
        stub
        + "\ndocument.getElementById('assessment-data').textContent = "
        + json.dumps(json.dumps(data))
        + ";\n"
        + initializer
        + note.group(0)
        + capture
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return dict(parsed)


def test_controller_initializer_matches_runtime_to_the_selected_task(tmp_path: Path) -> None:
    report = _report()
    attempts: list[dict[str, object]] = []
    for task, model, complete in (("grid", "model-A", False), ("selection", "model-B", True)):
        pair = _report()
        for index in (0, 1):
            attempt = _attempt(pair, index)
            attempt.update(id=f"{task}-{attempt['arm']}", task_id=task, model=model)
            stages = attempt["stages"]
            assert isinstance(stages, list) and isinstance(stages[0], dict)
            measurements = stages[0]["metrics"]
            assert isinstance(measurements, dict)
            values = measurements["fallow"]["metrics"]
            values["complexity_complete_for_declared_scope"] = complete
            attempts.append(attempt)
    report["attempts"] = attempts
    result = _controller_result(chart_data(report), tmp_path)
    assert result["task"] == "selection"
    runtime = result["runtime"]
    assert isinstance(runtime, dict) and runtime["model"] == "model-B"
    visible = result["visible"]
    assert isinstance(visible, list) and visible
    assert all(row["task"] == "selection" and row["model"] == "model-B" for row in visible)


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


def _draw_plot_result(
    data: dict[str, object], tmp_path: Path
) -> tuple[list[object], list[str], int]:
    controller = Path("src/pluginbench/assessment-charts.html").read_text().rsplit("<script>", 1)[1]
    function = re.search(r"function drawPlot\(\{.*?\n\}", controller, re.S)
    assert function is not None
    source = function.group(0)
    tick_formatter = re.search(r"const formatTick = [^\n]+", controller)
    assert tick_formatter is not None
    stub = r"""
const guidance = {value: 'candidate'}, stage = {value: 'handoff'};
const showRuns = {checked: true};
const unique = items => [...new Set(items)];
const format = value => String(value);
const armLabel = arm => arm === 'baseline' ? 'Vanilla' : arm;
const create = () => ({style: {}, clientWidth: 640});
const resolvedColor = () => '#222326';
const observationText = row => JSON.stringify(row);
const document = {body: {}};
const getComputedStyle = () => ({fontFamily: 'system-ui'});
let options;
const echarts = {init: () => ({setOption: value => {options = value;}})};
"""
    script = tmp_path / "draw-plot.js"
    script.write_text(
        "const data = "
        + json.dumps(data)
        + ";\n"
        + stub
        + tick_formatter.group(0)
        + "\n"
        + source
        + "\ndrawPlot({parent: {plots: []}, rows: data.observations.filter(row => row.role === 'handoff' && row.metric === 'Input tokens'), metric: 'Input tokens', panel: 'Tokens'});\n"
        + "console.log(JSON.stringify({bars: options.series.filter(row => row.type === 'bar').flatMap(row => row.data), labels: options.yAxis.data, points: options.series.filter(row => row.type === 'scatter').reduce((count, row) => count + row.data.length, 0)}));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    bars, labels, points = parsed["bars"], parsed["labels"], parsed["points"]
    assert isinstance(bars, list) and isinstance(labels, list) and isinstance(points, int)
    assert all(isinstance(label, str) for label in labels)
    return list(bars), [label for label in labels if isinstance(label, str)], points


def test_draw_plot_requires_vanilla_and_selected_skill_medians(tmp_path: Path) -> None:
    report = _report()
    report["attempts"] = [_attempt(report, 0)]
    baseline_only = chart_data(report)
    bars, labels, points = _draw_plot_result(baseline_only, tmp_path)
    assert all(value is None for value in bars)
    assert all("raw only" in label for label in labels)
    assert points == 1
    bars, labels, _ = _draw_plot_result(chart_data(_report()), tmp_path)
    assert sum(value is not None for value in bars) == 2
    assert all("raw only" not in label for label in labels)


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


def test_single_stage_rows_are_not_duplicated_and_final_can_match_handoff_fallback() -> None:
    report = _report()
    data = chart_data(report)
    rows = data["observations"]
    assert isinstance(rows, list) and len(rows) == 2 * sum(
        spec[2] not in {"review_work", "review_findings"} for spec in METRICS
    )
    assert data["distinct_final"] is False
    final = copy.deepcopy(_stage(report))
    final["id"] = "final"
    stages = _attempt(report)["stages"]
    assert isinstance(stages, list)
    stages.append(final)
    _attempt(report)["final"] = "final"
    data = chart_data(report)
    assert data["distinct_final"] is True
    assert data["single_stage_attempts"] == ["baseline"]
    medians = data["medians"]
    assert isinstance(medians, list)
    assert any(row["role"] == "final" and row["arm"] == "candidate" for row in medians)


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
    _, csv_text = chart_artifacts(report)
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


def test_outcomes_count_every_attempt_and_do_not_infer_review_acceptance() -> None:
    report = _report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    failed, missing = copy.deepcopy(_attempt(report)), copy.deepcopy(_attempt(report))
    failed["id"], missing["id"] = "failed", "missing"
    failed_stages, missing_stages = failed["stages"], missing["stages"]
    assert isinstance(failed_stages, list) and isinstance(failed_stages[0], dict)
    assert isinstance(missing_stages, list) and isinstance(missing_stages[0], dict)
    failed_stages[0]["correctness"] = {"status": "fail"}
    missing_stages[0].update(snapshot_digest=None, correctness={"status": "unknown"})
    attempts.extend([failed, missing])
    data = chart_data(report)
    candidate = next(row for row in _chart_rows(data, "outcomes") if row["arm"] == "candidate")
    assert candidate["assigned"] == 3
    assert candidate["handoff"] == candidate["final"] == {"pass": 1, "fail": 1, "unknown": 1}
    assert candidate["verified_issue_count"] is candidate["external_rounds"] is None
    assert "accepted" not in candidate and "winner" not in data


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


def test_selected_clean_review_is_visible_without_complete_trajectory() -> None:
    report = _report()
    attempt = _attempt(report)
    attempt["correction_burden"] = {
        "status": "incomplete",
        "verified_issue_count": 0,
        "external_rounds": 1,
    }
    attempt["independent_review"] = {"status": "reviewed", "stage": "final", "finding_count": 0}
    candidate = next(
        row for row in _chart_rows(chart_data(report), "outcomes") if row["arm"] == "candidate"
    )
    assert candidate["verified_issue_count"] is None
    assert candidate["selected_review_findings"] == 0
    assert candidate["selected_review_stages"] == ["final"]
    assert candidate["recorded_review_rounds"] == 1


def _add_repairs(report: dict[str, object]) -> list[dict[str, object]]:
    attempt = _attempt(report)
    stages = attempt["stages"]
    assert isinstance(stages, list)
    for identifier, input_tokens, output_tokens in (("repair-1", 40, 5), ("final", 60, 6)):
        stage = copy.deepcopy(stages[0])
        stage.update(
            id=identifier,
            work={
                "tokens": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "token_format": "native codex",
            },
        )
        stages.append(stage)
    attempt["final"] = "final"
    return stages


@pytest.mark.parametrize("missing", [None, "input", "work", "format"])
def test_final_tokens_sum_initial_and_all_repair_calls_without_inventing_usage(
    missing: str | None,
) -> None:
    report = _report()
    stages = _add_repairs(report)
    repair_work = stages[1]["work"]
    assert isinstance(repair_work, dict)
    repair_tokens = repair_work["tokens"]
    assert isinstance(repair_tokens, dict)
    if missing == "input":
        repair_tokens.pop("input_tokens")
    elif missing == "work":
        stages[1]["work"] = None
    elif missing == "format":
        repair_work["token_format"] = "another provider"
    data = chart_data(report)
    final = {
        row["metric"]: row
        for row in _chart_rows(data, "observations")
        if row["arm"] == "candidate" and row["role"] == "final" and row["panel"] == "Tokens"
    }
    assert final["Input tokens"]["value"] == (200 if missing is None else None)
    assert final["Output tokens"]["value"] == (None if missing in {"work", "format"} else 21)
    if missing not in {"work", "format"}:
        assert "total native tokens" in str(final["Output tokens"]["reason"])
    else:
        assert final["Output tokens"]["status"] == "unknown"
    handoff = [row for row in _observations(data, "Input tokens") if row["arm"] == "candidate"]
    assert handoff[0]["value"] == 100


def test_outcomes_keep_seed_runtime_and_delivery_separate() -> None:
    report = _report()
    _attempt(report)["before_digest"] = "other-seed"
    _attempt(report)["provenance"] = {"host": "codex", "guidance_delivery": "forced-text"}
    data = chart_data(report)
    assert len(_chart_rows(data, "outcomes")) == 2
    candidate = next(row for row in _chart_rows(data, "outcomes") if row["arm"] == "candidate")
    assert candidate["before_digest"] == "other-seed"
    assert candidate["guidance_delivery"] == ["forced-text"]
    assert not _matched(data, "Input tokens")


def test_old_coverage_without_declared_population_proof_stays_raw() -> None:
    report = _report()
    _metric_values(report, "coverage").pop("population_complete_for_declared_scope")
    data = chart_data(report)
    assert not _matched(data, "Branch coverage %")
    row = next(row for row in _observations(data, "Branch coverage %") if row["arm"] == "candidate")
    assert row["value"] == 75 and "population completeness is unproven" in str(row["reason"])


def _rendered_chart_summary(
    data: dict[str, object], tmp_path: Path
) -> tuple[list[list[str]], list[str]]:
    controller = Path("src/pluginbench/assessment-charts.html").read_text().rsplit("<script>", 1)[1]
    function = re.search(r"function showOutcomes\(\) \{.*?\n\}", controller, re.S)
    assert function is not None
    panel_function = re.search(r"function renderPanel\(\{.*?\n\}", controller, re.S)
    assert panel_function is not None
    empty_population = re.search(r"const emptyFunctionMaximum = .*?;\n", controller)
    assert empty_population is not None
    script = tmp_path / "outcomes-controller.js"
    script.write_text(
        "const data = "
        + json.dumps(data)
        + ";\n"
        + r"""
const nodes = [];
const create = (tag, text, parent) => {
  const node = {tag, text, children: [], attributes: {}, append(child) {this.children.push(child)}, setAttribute(name, value) {this.attributes[name] = value}};
  nodes.push(node); if (parent) parent.append(node); return node;
};
const document = {getElementById: () => ({replaceChildren() {}, append() {}})};
const unique = items => [...new Set(items)];
const format = value => value === null ? 'Unknown' : String(value);
const armLabel = arm => arm === 'baseline' ? 'Vanilla' : arm;
const runtimeKey = row => JSON.stringify([row.model, row.host, row.harness, row.harness_version, row.reasoning_effort, row.requested_reasoning_effort, row.runtime_source]);
const taskSelect = {value: ''}, guidance = {value: 'candidate'};
const runtime = {value: runtimeKey(data.outcomes.find(row => row.arm === 'baseline'))};
const observations = data.observations, stage = {value: 'handoff'};
const selectedRole = row => row.role === 'handoff';
const drawPlot = () => 0, addDetails = () => {};
"""
        + function.group(0)
        + empty_population.group(0)
        + panel_function.group(0)
        + "\nshowOutcomes(); renderPanel({section: create('section'), panel: 'Tokens', metric: 'Input tokens'}); console.log(JSON.stringify({rows: nodes.filter(node => node.tag === 'tr').map(node => node.children.map(child => child.text)), notes: nodes.filter(node => node.className === 'counts').map(node => node.text)}));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    rows, notes = rendered["rows"], rendered["notes"]
    assert isinstance(rows, list)
    assert isinstance(notes, list)
    return [[str(value) for value in row] for row in rows], [str(note) for note in notes]


@pytest.mark.parametrize(
    "actual,version,correctness,state",
    [
        (None, None, "fail", "complete"),
        (None, None, "unknown", "pending"),
        ("wrong-model", "0.162.0", "fail", "complete"),
        ("exact-model", "9.9.9", "fail", "complete"),
    ],
)
def test_failed_or_mismatched_execution_remains_in_assigned_runtime_denominator(
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
    candidate = next(row for row in _chart_rows(data, "outcomes") if row["arm"] == "candidate")
    assert candidate["assigned"] == 2
    assert candidate["handoff"] == {
        "pass": 1,
        "fail": int(correctness == "fail"),
        "unknown": int(correctness == "unknown"),
    }
    assert candidate["actual_runtime_unknown"] == (0 if actual else 1)
    assert candidate["actual_runtime_mismatch"] == (1 if actual else 0)
    rows, notes = _rendered_chart_summary(data, tmp_path)
    candidate_cells = next(row for row in rows if row[1] == "candidate")
    assert (
        candidate_cells[3]
        == candidate_cells[4]
        == f"1 / 2 · 1 {'failed' if correctness == 'fail' else 'unknown'}"
    )
    assert candidate_cells[2] == ("2 complete" if state == "complete" else "1 complete · 1 pending")
    assert f"{0 if actual else 1} unknown" in candidate_cells[-1]
    assert any("candidate: 1/2 measured attempts" in note for note in notes)
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


def test_legacy_outcomes_do_not_pool_unknown_execution_into_known_runtime() -> None:
    report = _report()
    attempts = report["attempts"]
    assert isinstance(attempts, list)
    unknown = copy.deepcopy(_attempt(report))
    unknown.update(id="unknown-legacy", model=None)
    attempts.append(unknown)
    candidate_rows = [
        row for row in _chart_rows(chart_data(report), "outcomes") if row["arm"] == "candidate"
    ]
    assert len(candidate_rows) == 2
    assert all(row["assigned"] == 1 and row["assignment_bound"] is False for row in candidate_rows)


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
            "coverage": {"status": "unavailable", "metrics": None},
        }
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
    html, csv_text = chart_artifacts(report)
    assert "</script><script>alert" not in html
    assert "\\u003c/script>" in html
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert any(row["attempt_id"] == "'=cmd()" for row in rows)
    controller = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "textContent" in controller and "innerHTML" not in controller
    assert re.search(r"<script[^>]+src=", html) is None
    assert "__ECHARTS_SCRIPT__" not in html and "echarts.init" in controller


def test_verified_demo_writer_emits_offline_charts_and_keeps_return_api(tmp_path: Path) -> None:
    report = build_assessment(Path("tests/fixtures/standards-assessment/study.yaml"))
    json_path, markdown_path = write_assessment(report, tmp_path)
    assert json_path.exists() and markdown_path.exists()
    html = (tmp_path / "assessment-charts.html").read_text()
    payload = re.search(
        r'<script id="assessment-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert payload is not None
    data = json.loads(payload.group(1))
    assert data["assigned"] == 1 and data["distinct_final"] is True
    assert data["limitations"] == report["limitations"]
    assert "Test overlap" in {row["panel"] for row in data["observations"]}
    assert not data["medians"]  # Unlabelled demo identities are never inferred as baseline.
    raw_loc = [row for row in data["observations"] if row["metric"] in {"Source LOC", "Test LOC"}]
    assert len(raw_loc) == 4  # Two measured LOC subtypes at handoff and final.
    assert all(row["arm"] == row["model"] == "unknown" for row in raw_loc)
    assert all(row["value"] is not None and row["compatibility"] is None for row in raw_loc)
    script = tmp_path / "chart-script.js"
    script.write_text(html.rsplit("<script>", 1)[1].split("</script>", 1)[0])
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("name", ["assessment-charts.html", "assessment-charts.csv"])
def test_chart_symlinks_reject_before_any_report_write(tmp_path: Path, name: str) -> None:
    report = build_assessment(Path("tests/fixtures/standards-assessment/study.yaml"))
    external = tmp_path / "external"
    external.write_text("untouched")
    output = tmp_path / "output"
    output.mkdir()
    (output / name).symlink_to(external)
    with pytest.raises(ValueError, match="symlinks"):
        write_assessment(report, output)
    assert external.read_text() == "untouched"
    assert not (output / "assessment-report.json").exists()


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


def test_legacy_review_severity_is_unknown_instead_of_assumed_medium() -> None:
    report = _fixed_review_report()
    review = _fixed_review(report)
    review.update(
        issues=[{"issue_id": "legacy", "category": "tests"}],
        finding_count=1,
        severity_counts=None,
        category_counts={"correctness": 0, "scope": 0, "maintainability": 0, "tests": 1},
    )
    data = chart_data(report)
    assert _observations(data, "Total review findings")[1]["value"] == 1
    assert _observations(data, "Tests review findings")[1]["value"] == 1
    assert _observations(data, "P2 review findings")[1]["value"] is None
    review["criteria_sha256"] = None
    assert _observations(chart_data(report), "Total review findings")[1]["value"] is None


def test_different_fixed_review_criteria_have_no_comparison_bars() -> None:
    report = _fixed_review_report()
    _fixed_review(report)["criteria_sha256"] = "sha256:" + "b" * 64
    data = chart_data(report)
    rows = _observations(data, "Total review findings")
    assert rows[0]["compatibility"] != rows[1]["compatibility"]
    assert rows[1]["value"] == 0 and rows[1]["status"] == "not comparable"
    assert not _matched(data, "Total review findings")


def test_review_counts_use_fixed_reviewed_stage_at_handoff_and_final() -> None:
    report = _fixed_review_report()
    for index in (0, 1):
        attempt = _attempt(report, index)
        stages = attempt["stages"]
        assert isinstance(stages, list)
        final = copy.deepcopy(_stage(report, index))
        final.update(id="final", snapshot_digest="final-digest", correctness={"status": "fail"})
        stages.append(final)
        attempt["final"] = "final"
    rows = [
        row
        for row in _chart_rows(chart_data(report), "observations")
        if row["metric"] == "Total review findings"
    ]
    assert len(rows) == 4
    assert {row["role"] for row in rows} == {"handoff", "final"}
    assert all(row["stage"] == "handoff" and row["snapshot_digest"] == "snapshot" for row in rows)
    assert all(row["value"] == 0 and row["correctness"] == "pass" for row in rows)


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
    html, _ = chart_artifacts(report)
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
        (None, None, True),
        (None, "a" * 64, False),
    ],
)
def test_review_token_comparisons_require_same_criteria_when_recorded(
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
        assert (rows[0]["compatibility"] == rows[1]["compatibility"]) is comparable
