"""Compact, selected views of canonical evidence for one evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from pluginbench.assessment_charts import (
    _map,
    _number,
    _rows,
    chart_artifacts_from_data,
    chart_data,
)

MEASUREMENT_METRICS = {
    "tokens": ("Input tokens", "Output tokens"),
    "lines": (
        "Source lines added",
        "Source lines removed",
        "Test lines added",
        "Test lines removed",
    ),
    "cyclomatic": (
        "After changed-function cyclomatic maximum",
        "New-file function cyclomatic maximum",
    ),
    "cognitive": (
        "After changed-function cognitive maximum",
        "New-file function cognitive maximum",
    ),
    "dead_code": ("Introduced dead findings",),
    "coverage": ("Branch coverage %", "Function coverage %"),
    "test_overlap": ("Zero marginal branch contribution cases",),
    "review": (
        "Total review findings",
        "P0 review findings",
        "P1 review findings",
        "P2 review findings",
        "P3 review findings",
        "Correctness review findings",
        "Scope review findings",
        "Maintainability review findings",
        "Tests review findings",
        "Judge input tokens",
        "Judge output tokens",
    ),
}
OBSERVATION_FIELDS = (
    "attempt_id",
    "task",
    "arm",
    "panel",
    "metric",
    "family",
    "value",
    "unit",
    "correctness",
    "status",
    "reason",
    "compatibility",
    "model",
    "host",
    "harness",
    "harness_version",
    "reasoning_effort",
    "requested_reasoning_effort",
    "runtime_source",
    "role",
    "stage",
    "snapshot_digest",
    "before_digest",
    "per_test_count",
    "elapsed_seconds",
    "provenance",
)
PROVENANCE_FIELDS = (
    "tool",
    "tool_version",
    "method",
    "config_digest",
    "scope",
    "token_format",
    "clock",
    "complete_for_declared_scope",
    "reported_functions",
    "limitations",
    "test_scope",
    "covered",
    "total",
    "selection_policy",
    "branch_denominator_digest",
    "function_denominator_digest",
    "judge_model",
    "judge_harness",
    "judge_reasoning_effort",
    "judge_comparison_reasoning_effort",
    "judge_effort_evidence_source",
    "judge_harness_version",
    "review_stage",
    "criteria_sha256",
    "reasoning_comparison",
)

COMPLETENESS_COUNTS = {
    "tokens": "Input/output tokens",
    "lines": "Source/test lines added/removed",
    "cognitive": "Complexity population",
    "cyclomatic": "Complexity population",
    "dead_code": "Introduced dead findings",
    "coverage": "Coverage covered/total counts",
    "test_overlap": "Test-overlap candidate counts",
}


def _selected_completeness(
    *, data: dict[str, object], projection: dict[str, object], selected: list[object]
) -> list[dict[str, object]]:
    source = {
        row["attempt_id"]: row
        for row in _rows(projection["measurement_completeness"])
        if row["role"] == "handoff"
    }
    observations = _rows(data["observations"])
    required = {COMPLETENESS_COUNTS[str(family)] for family in selected if family != "review"}
    results = []
    for attempt in _rows(data["attempts"]):
        record = source.get(attempt["id"], {})
        measured = {
            row["metric"]: row for row in observations if row["attempt_id"] == attempt["id"]
        }

        def captured(metrics: tuple[str, ...]) -> bool:
            return all(
                metric in measured and measured[metric]["value"] is not None for metric in metrics
            )

        def captured_population(metric: str) -> bool:
            row = measured.get(metric, {})
            provenance = _map(row.get("provenance"))
            count = _number(provenance.get("reported_functions"))
            return (
                provenance.get("complete_for_declared_scope") is True
                and count is not None
                and (
                    row.get("value") is None
                    if count == 0
                    else _number(row.get("value")) is not None
                )
            )

        counts = {name: _map(record.get("counts")).get(name) is True for name in sorted(required)}
        for family in ("tokens", "lines"):
            if family in selected and not record:
                counts[COMPLETENESS_COUNTS[family]] = captured(MEASUREMENT_METRICS[family])
        complexity_metrics = [
            metric
            for family in ("cognitive", "cyclomatic")
            if family in selected
            for metric in MEASUREMENT_METRICS[family]
        ]
        if complexity_metrics:
            counts["Complexity population"] = all(
                captured_population(metric) for metric in complexity_metrics
            )
        if "review" in selected:
            counts["Review findings"] = captured(
                tuple(
                    metric
                    for metric in MEASUREMENT_METRICS["review"]
                    if not metric.startswith("Judge ")
                )
            )
            counts["Review input/output tokens"] = captured(
                ("Judge input tokens", "Judge output tokens")
            )
        results.append(
            {
                **record,
                "attempt_id": attempt["id"],
                "task": attempt["task"],
                "arm": attempt["guidance"],
                "role": "handoff",
                "counts": counts,
                "complete": bool(counts) and all(counts.values()),
            }
        )
    return results


def _assigned_counts(
    observations: list[dict[str, object]],
) -> dict[tuple[object, object, object], int]:
    attempts: dict[tuple[object, object, object], set[object]] = {}
    for row in observations:
        key = (row.get("task"), row.get("metric"), row.get("arm"))
        attempts.setdefault(key, set()).add(row["attempt_id"])
    return {key: len(ids) for key, ids in attempts.items()}


def _comparison_rows(data: dict[str, object]) -> list[dict[str, object]]:
    observations = _rows(data["observations"])
    assigned_counts = _assigned_counts(observations)
    medians = _rows(data["medians"])
    lookup = {
        (row.get("task"), row.get("metric"), row.get("compatibility"), row.get("arm")): row
        for row in medians
    }
    profiles = {
        (row.get("task"), row.get("metric"), row.get("arm"), row.get("compatibility"))
        for row in observations
        if row.get("arm") not in {"baseline", "unknown"}
    }
    results = []
    for task, metric, arm, compatibility in sorted(profiles, key=str):
        baseline = lookup.get((task, metric, compatibility, "baseline"), {})
        guided = lookup.get((task, metric, compatibility, arm), {})
        baseline_value, guided_value = baseline.get("value"), guided.get("value")
        absolute = (
            float(guided_value) - float(baseline_value)
            if compatibility is not None
            and isinstance(baseline_value, (int, float))
            and isinstance(guided_value, (int, float))
            else None
        )
        known = absolute is not None
        percentage = (
            absolute / float(baseline_value) * 100
            if absolute is not None
            and isinstance(baseline_value, (int, float))
            and baseline_value != 0
            else None
        )
        assigned = {
            group: assigned_counts.get((task, metric, group), 0) for group in ("baseline", arm)
        }
        baseline_measured, guided_measured = baseline.get("measured", 0), guided.get("measured", 0)
        complete = baseline_measured == assigned["baseline"] and guided_measured == assigned[arm]
        metric_guide = _map(_map(data.get("metric_guide")).get(str(metric)))
        unit = metric_guide.get("unit")
        results.append(
            {
                "task": task,
                "absolute_delta_unit": "percentage points" if unit == "% covered" else unit,
                "metric": metric,
                "guidance": arm,
                "compatibility": compatibility,
                "baseline_median": baseline_value,
                "guidance_median": guided_value,
                "absolute_delta_vs_vanilla": absolute,
                "percentage_delta_vs_vanilla": percentage,
                "baseline_assigned": assigned["baseline"],
                "baseline_measured": baseline_measured,
                "guidance_assigned": assigned[arm],
                "guidance_measured": guided_measured,
                "status": "complete" if known and complete else "partial" if known else "unknown",
                "reason": "Guidance median minus this evaluation's vanilla median; lower is not automatically better."
                if known
                else "No matching measured vanilla and guidance profile; unknown is not zero.",
                "percentage_reason": "Undefined when vanilla is zero or either median is unknown."
                if percentage is None
                else None,
            }
        )
    return results


def _aggregate_rows(data: dict[str, object]) -> list[dict[str, object]]:
    """Give each assigned task one median, independent of its repeat count."""
    observations = _rows(data["observations"])
    assigned_counts = _assigned_counts(observations)
    medians = _rows(data["medians"])
    raw_medians = _rows(data["raw_medians"])
    comparisons = _rows(data["comparisons"])
    median_rows: dict[tuple[object, object, object], list[dict[str, object]]] = {}
    raw_rows: dict[tuple[object, object, object], list[dict[str, object]]] = {}
    comparison_rows: dict[tuple[object, object, object], list[dict[str, object]]] = {}
    for row in medians:
        median_rows.setdefault((row["task"], row["metric"], row["arm"]), []).append(row)
    for row in raw_medians:
        raw_rows.setdefault((row["task"], row["metric"], row["arm"]), []).append(row)
    for row in comparisons:
        if row["absolute_delta_vs_vanilla"] is not None:
            comparison_rows.setdefault((row["task"], row["metric"], row["guidance"]), []).append(
                row
            )
    tasks = sorted({str(row["task"]) for row in _rows(data["attempts"])})
    arms = sorted(
        {str(row["arm"]) for row in observations}, key=lambda arm: (arm != "baseline", arm)
    )
    metrics = sorted({str(row["metric"]) for row in observations})
    results = []
    for metric in metrics:
        baseline_values = {task: median_rows.get((task, metric, "baseline"), []) for task in tasks}
        for arm in arms:
            task_values = []
            task_statuses = []
            comparison_statuses = []
            matching_baselines = []
            for task in tasks:
                rows = raw_rows.get((task, metric, arm), [])
                value = rows[0].get("value") if len(rows) == 1 else None
                task_values.append({"task": task, "value": value})
                matches = comparison_rows.get((task, metric, arm), [])
                baseline = baseline_values[task]
                measured = rows[0].get("measured", 0) if len(rows) == 1 else 0
                assigned = assigned_counts.get((task, metric, arm), 0)
                raw_status = "complete" if value is not None and measured == assigned else "partial"
                task_statuses.append(raw_status)
                if arm == "baseline":
                    matching_baselines.append(value)
                    comparison_statuses.append(raw_status)
                else:
                    match = matches[0] if len(matches) == 1 and len(baseline) == 1 else {}
                    matching_baselines.append(match.get("baseline_median"))
                    comparison_statuses.append(str(match.get("status", "unknown")))
            known_values = [
                float(row["value"]) for row in task_values if isinstance(row["value"], (int, float))
            ]
            value = sum(known_values) / len(known_values) if known_values else None
            known_baselines = [
                float(value) for value in matching_baselines if isinstance(value, (int, float))
            ]
            all_tasks_matched = len(known_values) == len(known_baselines) == len(tasks) and bool(
                tasks
            )
            baseline_value = sum(known_baselines) / len(tasks) if all_tasks_matched else None
            delta = (
                value - baseline_value if value is not None and baseline_value is not None else None
            )
            guide = _map(_map(data["metric_guide"]).get(metric))
            results.append(
                {
                    "metric": metric,
                    "guidance": arm,
                    "value": value,
                    "unit": guide.get("unit"),
                    "method": "equal-weight mean of per-task medians",
                    "assigned_tasks": len(tasks),
                    "measured_tasks": len(known_values),
                    "task_medians": task_values,
                    "status": "complete"
                    if len(known_values) == len(tasks)
                    and all(status == "complete" for status in task_statuses)
                    else "partial"
                    if known_values
                    else "unknown",
                    "comparison_status": "complete"
                    if all_tasks_matched
                    and all(status == "complete" for status in comparison_statuses)
                    else "partial"
                    if all_tasks_matched
                    else "unknown",
                    "baseline_value": baseline_value,
                    "absolute_delta_vs_vanilla": delta,
                    "percentage_delta_vs_vanilla": delta / baseline_value * 100
                    if delta is not None and baseline_value
                    else None,
                    "absolute_delta_unit": "percentage points"
                    if guide.get("unit") == "% covered"
                    else guide.get("unit"),
                    "reason": "Each task has equal weight, regardless of repetitions. Deltas require matching vanilla and guidance medians for every assigned task. Partial raw averages describe the measured tasks only.",
                }
            )
    return results


def evaluation_data(report: dict[str, object], config: dict[str, object]) -> dict[str, object]:
    """Select measurements and derive baseline comparisons without scanning or model calls."""
    selected = config.get("measurements")
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(name, str) or name not in MEASUREMENT_METRICS for name in selected)
    ):
        raise ValueError("measurements must select supported measurement families")
    families = {metric: family for family in selected for metric in MEASUREMENT_METRICS[family]}
    projection_source = (
        {**report, "independent_review": {"status": "unknown"}}
        if "review" in selected and not report.get("independent_review")
        else report
    )
    projection = chart_data(projection_source)
    guide = _map(projection["metric_guide"])
    observations = []
    for source in _rows(projection["observations"]):
        if source.get("metric") not in families:
            continue
        metric = str(source["metric"])
        row = {field: source.get(field) for field in OBSERVATION_FIELDS}
        row.update(family=families[metric], unit=_map(guide.get(metric)).get("unit"))
        row["provenance"] = {
            key: value
            for key, value in _map(source.get("provenance")).items()
            if key in PROVENANCE_FIELDS
        }
        observations.append(row)
    attempts = []
    origins = _map(report.get("baseline_origin"))
    for attempt in _rows(report.get("attempts")):
        stage = next(
            row for row in _rows(attempt.get("stages")) if row.get("id") == attempt.get("handoff")
        )
        provenance = _map(attempt.get("provenance"))
        attempts.append(
            {
                "id": attempt.get("id"),
                "repeat": attempt.get("attempt"),
                "task": attempt.get("task_id"),
                "guidance": attempt.get("arm"),
                "baseline_origin": {
                    key: _map(origins.get(str(attempt.get("id")))).get(key)
                    for key in ("cache_key", "origin_run", "origin_attempt", "evidence")
                }
                if str(attempt.get("id")) in origins
                else None,
                "correctness": _map(stage.get("correctness")).get("status", "unknown"),
                "actual_model": attempt.get("model"),
                "runtime": {
                    name: provenance.get(name)
                    for name in (
                        "host",
                        "harness",
                        "harness_version",
                        "reasoning_effort",
                        "requested_reasoning_effort",
                        "effort_evidence_source",
                    )
                },
                "requested_model": provenance.get(
                    "assigned_model", _map(config.get("runner")).get("model")
                ),
                "artifacts": {
                    "execution": _map(stage.get("execution_provenance")).get("artifact"),
                    "snapshot_digest": stage.get("snapshot_digest"),
                    "before_digest": attempt.get("before_digest"),
                },
            }
        )
    limitations = [
        "Each guidance set is compared with the same vanilla cohort. Compare separate evaluations through compatible baseline deltas.",
        "Deltas are differences in observed medians, not a winner or composite quality score. A negative delta is not automatically better.",
    ]
    if origins:
        limitations.append(
            f"{len(origins)} vanilla attempts were reused from earlier evaluations. Their tokens and time are original observations, not fresh independent samples. Grading, measurements, and reviews run again."
        )
    if {"cyclomatic", "cognitive"}.intersection(selected):
        limitations.append(
            "Complexity covers functions touched by changed lines and functions in new files, as separate populations. Empty populations have no maximum."
        )
    if {"coverage", "test_overlap"}.intersection(selected):
        limitations.append(
            "Coverage and test-overlap counts are proxies, not proof of correct assertions or unnecessary tests."
        )
    if "review" in selected:
        limitations.append(
            "AI review findings are judgments, not deterministic defect counts. The reviewer is not calibrated against human judgments; recorded reviews do not measure human review churn."
        )
    if config.get("review_projection") is not None:
        limitations.append(
            "This report selects a separate review revision. Judge charts show that revision's usage; bound prior review receipts retain earlier calls, failures, and usage."
        )
    data = {
        "schema_version": 1,
        "kind": "evaluation",
        "name": report.get("name"),
        "comparison_identity": config.get("comparison_identity"),
        "guidance_packages": config.get("guidance_packages", {}),
        "artifact_base": config.get("artifact_base", "."),
        "runner": config.get("runner"),
        "jobs": config.get("jobs", 1),
        "reviewer": config.get("reviewer") if "review" in selected else None,
        "measurements": selected,
        "measurement_collection": config.get("measurement_collection"),
        "assigned": len(attempts),
        "coding_invocations": _map(report.get("study_progress")).get("native_invocations"),
        "reused_baseline_attempts": len(origins),
        "attempts": attempts,
        "observations": observations,
        "observation_fields": list(OBSERVATION_FIELDS),
        "medians": [row for row in _rows(projection["medians"]) if row.get("metric") in families],
        "raw_medians": [
            row for row in _rows(projection["raw_medians"]) if row.get("metric") in families
        ],
        "metric_guide": {metric: guide[metric] for metric in families},
        "independent_review": projection.get("independent_review")
        if "review" in selected
        else None,
        "review_findings": projection.get("review_findings") if "review" in selected else [],
        "download_csv": "results.csv",
        "limitations": limitations,
    }
    data["comparisons"] = _comparison_rows(data)
    data["aggregates"] = _aggregate_rows(data)
    data["measurement_completeness"] = _selected_completeness(
        data=data, projection=projection, selected=selected
    )
    if config.get("review_projection") is not None:
        data["review_projection"] = config["review_projection"]
    return data


def write_evaluation_report(
    report: dict[str, object], config: dict[str, object], out_dir: Path
) -> Path:
    if out_dir.is_symlink() or any(
        (out_dir / name).is_symlink() for name in ("results.json", "results.csv", "report.html")
    ):
        raise ValueError("report outputs must not be symlinks")
    data = evaluation_data(report, config)
    html, csv_text = chart_artifacts_from_data(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = out_dir / "results.json"
    result.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
    (out_dir / "results.csv").write_text(csv_text)
    (out_dir / "report.html").write_text(html)
    return result
