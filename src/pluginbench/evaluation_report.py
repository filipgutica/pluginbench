"""Compact, selected views of canonical evidence for one evaluation."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from pluginbench.measurement_profiles import REMOVED_TEST_OVERLAP
from pluginbench.assessment_charts import (
    DIAGNOSTIC_PANELS,
    METRICS,
    _map,
    _net_lines,
    _number,
    _rows,
    _signed,
    chart_artifacts_from_data,
    chart_data,
    execution_outcome,
)

MEASUREMENT_METRICS = {
    "tokens": (
        "Input tokens",
        "Cached input tokens",
        "Uncached input tokens",
        "Output tokens",
        "Reasoning output tokens",
        "Coding time",
    ),
    "lines": (
        "Source lines added",
        "Source lines removed",
        "Source lines net",
        "Test lines added",
        "Test lines removed",
        "Duplicated added test lines",
    ),
    "cyclomatic": (
        "Net cyclomatic complexity added",
        "Functions added",
        "Source files added",
    ),
    "cognitive": (
        "Net cognitive complexity added",
        "Complexity hotspots added",
    ),
    "dead_code": ("Introduced dead findings", "Introduced clone groups"),
    "coverage": (
        "Changed-line coverage %",
        "Changed-branch coverage %",
        "Redundant added tests",
        "Line coverage %",
        "Statement coverage %",
        "Branch coverage %",
        "Function coverage %",
    ),
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
    "language",
    "analyzers",
    "metric_settings",
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
    "line_denominator_digest",
    "statement_denominator_digest",
    "judge_model",
    "judge_harness",
    "judge_reasoning_effort",
    "judge_comparison_reasoning_effort",
    "judge_effort_evidence_source",
    "judge_harness_version",
    "review_stage",
    "criteria_sha256",
    "reasoning_comparison",
    "execution_status",
    "execution_reason",
)

COMPLETENESS_COUNTS = {
    "tokens": "Input/output tokens",
    "lines": "Source/test lines added/removed",
    "cognitive": "Complexity population",
    "cyclomatic": "Complexity population",
    "dead_code": "Introduced dead findings",
    "coverage": "Coverage covered/total counts",
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

        counts = {name: _map(record.get("counts")).get(name) is True for name in sorted(required)}
        # Same populations as the collector-backed projection count: totals and line changes only.
        for family, metrics in (
            ("tokens", ("Input tokens", "Output tokens")),
            ("lines", MEASUREMENT_METRICS["lines"][:4]),
        ):
            if family in selected and not record:
                counts[COMPLETENESS_COUNTS[family]] = captured(metrics)
        complexity_metrics = [
            metric
            for family in ("cognitive", "cyclomatic")
            if family in selected
            for metric in MEASUREMENT_METRICS[family]
        ]
        if complexity_metrics:
            # Code-added sums are defined (zero) for complete empty populations; unknown stays None.
            counts["Complexity population"] = captured(tuple(complexity_metrics))
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
            # A percentage of a zero or negative baseline has no meaning.
            and baseline_value > 0
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


def _aggregate_groups(data: dict[str, object]) -> list[tuple[str, str | None, list[str]]]:
    tasks = sorted({str(row["task"]) for row in _rows(data["attempts"])})
    observations = _rows(data["observations"])
    groups: dict[tuple[str, str | None], set[str]] = {}
    task_analyzers: dict[tuple[str, str], set[str]] = {}
    unbound_values: set[tuple[str, str]] = set()
    for row in observations:
        group = None
        if row.get("family") in {"cognitive", "cyclomatic", "dead_code"}:
            known = task_analyzers.setdefault((str(row["metric"]), str(row["task"])), set())
            provenance = _map(row.get("provenance"))
            analyzers = _map(provenance.get("analyzers"))
            language = str(
                provenance.get("language")
                or ("JS/TS" if provenance.get("tool") == "fallow" else "unknown language")
            )
            family = str(row.get("family"))
            tool_name = (
                {"python": "vulture", "go": "staticcheck", "rust": "rustc"}.get(language, "fallow")
                if family == "dead_code"
                else "go_analysis"
                if language == "go"
                else "rca"
            )
            tool = _map(analyzers.get(tool_name))
            version = tool.get("version", provenance.get("tool_version"))
            if not version or version == "unknown" or (not tool and not provenance.get("tool")):
                if row.get("value") is not None:
                    unbound_values.add((str(row["metric"]), str(row["task"])))
                continue
            version = str(version)
            if tool_name == "go_analysis" and "gocognit=" in version:
                part = "gocognit=" if family == "cognitive" else "gocyclo="
                name = part[:-1] + " " + version.split(part)[-1].split()[0]
            else:
                name = (
                    version
                    if tool
                    else str(provenance.get("tool", "unknown analyzer")) + " " + version
                )
            group = f"{language.title()} · {name}"
            known.add(group)
            continue
        groups.setdefault((str(row["metric"]), group), set()).add(str(row["task"]))
    for (metric, task), known in task_analyzers.items():
        group = (
            "Conflicting analyzers"
            if len(known) > 1
            else next(iter(known))
            if known and (metric, task) not in unbound_values
            else "Unknown analyzer"
        )
        groups.setdefault((metric, group), set()).add(task)
    return [
        (metric, group, sorted(members) if group else tasks)
        for (metric, group), members in sorted(groups.items(), key=str)
    ]


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
    arms = sorted(
        {str(row["arm"]) for row in observations}, key=lambda arm: (arm != "baseline", arm)
    )
    results = []
    for metric, analysis_group, tasks in _aggregate_groups(data):
        unresolved_analyzer = analysis_group in {"Unknown analyzer", "Conflicting analyzers"}
        baseline_values = {task: median_rows.get((task, metric, "baseline"), []) for task in tasks}
        for arm in arms:
            task_values = []
            task_statuses = []
            comparison_statuses = []
            matching_baselines = []
            for task in tasks:
                rows = [] if unresolved_analyzer else raw_rows.get((task, metric, arm), [])
                value = rows[0].get("value") if len(rows) == 1 else None
                task_values.append({"task": task, "value": value})
                matches = (
                    [] if unresolved_analyzer else comparison_rows.get((task, metric, arm), [])
                )
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
                    "analysis_group": analysis_group,
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
                    "reason": "Analyzer provenance is missing or conflicting; overview values are unavailable. Raw observations and task medians remain separate."
                    if unresolved_analyzer
                    else "Each task has equal weight, regardless of repetitions. Deltas require matching vanilla and guidance medians for every assigned task. Partial raw averages describe the measured tasks only.",
                }
            )
    return results


REFERENCE_METRICS = (
    "Net cognitive complexity added",
    "Net cyclomatic complexity added",
    "Functions added",
    "Source files added",
    "Complexity hotspots added",
    "Introduced dead findings",
    "Introduced clone groups",
    "Source lines added",
    "Source lines removed",
    "Source lines net",
)


def _reference_values(report: dict[str, object]) -> list[dict[str, object]]:
    """Code measures of each task's reference solution, measured like a submission."""
    specs = {spec[1]: spec for spec in METRICS}
    rows: list[dict[str, object]] = []
    for task, value in sorted(_map(report.get("references")).items()):
        reference = _map(value)
        comparison = _map(reference.get("comparison"))
        static = (
            "analysis"
            if _map(comparison.get("analysis")).get("status") == "available"
            else "fallow"
        )
        for metric in REFERENCE_METRICS:
            _, _, kind, key = specs[metric]
            number = None
            if reference.get("status") == "available":
                if kind == "fallow_change":
                    section, field = key.split(".")
                    detail = _map(_map(comparison.get(static)).get(section))
                    number = (
                        _signed(detail.get(field)) if detail.get("status") == "available" else None
                    )
                elif kind == "loc_change":
                    size = _map(reference.get("code_size"))
                    number = (
                        _net_lines(size) if key == "source_lines_net" else _number(size.get(key))
                    )
            rows.append(
                {
                    "task": task,
                    "metric": metric,
                    "value": number,
                    "status": "measured" if number is not None else "unknown",
                    "reason": None
                    if number is not None
                    else str(reference.get("reason") or "reference measurement unavailable"),
                }
            )
    return rows


def _reference_comparisons(data: dict[str, object]) -> list[dict[str, object]]:
    """Attempt task medians as a ratio to the reference; a zero reference reports a difference."""
    references = {(row["task"], row["metric"]): row for row in _rows(data["reference_values"])}
    rows: list[dict[str, object]] = []
    for median in _rows(data["raw_medians"]):
        reference = references.get((median.get("task"), median.get("metric")))
        if median.get("role") != "handoff" or reference is None:
            continue
        value, base = _signed(median.get("value")), _signed(reference.get("value"))
        ratio = difference = None
        if value is not None and base is not None:
            difference = value - base
            # A ratio is only meaningful against a positive reference; otherwise report a difference.
            ratio = value / base if base > 0 else None
        rows.append(
            {
                "task": median["task"],
                "guidance": median["arm"],
                "metric": median["metric"],
                "value": value,
                "reference": base,
                "ratio_to_reference": ratio,
                "difference_vs_reference": difference,
                "status": "unknown"
                if difference is None
                else "ratio"
                if ratio is not None
                else "difference",
            }
        )
    return rows


def _reference_aggregates(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Equal-weight mean of per-task ratios; tasks without a defined ratio stay out and are counted."""
    groups: dict[tuple[str, str], dict[str, list[float | None]]] = {}
    for row in rows:
        tasks = groups.setdefault((str(row["guidance"]), str(row["metric"])), {})
        tasks.setdefault(str(row["task"]), []).append(_signed(row["ratio_to_reference"]))
    result: list[dict[str, object]] = []
    for (guidance, metric), tasks in sorted(groups.items()):
        # A task with several incompatible profiles has no single ratio.
        ratios = [
            values[0] for values in tasks.values() if len(values) == 1 and values[0] is not None
        ]
        result.append(
            {
                "guidance": guidance,
                "metric": metric,
                "ratio_to_reference": sum(ratios) / len(ratios) if ratios else None,
                "measured_tasks": len(ratios),
                "assigned_tasks": len(tasks),
                "method": "equal-weight mean of per-task median ÷ reference",
            }
        )
    return result


def _passing_summaries(
    data: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Task medians over passing attempts only, and equal-weight task means per analyzer group."""
    groups: dict[tuple[str, str, str], dict[object, list[float]]] = {}
    arms = sorted({str(row["arm"]) for row in _rows(data["observations"])})
    for row in _rows(data["observations"]):
        value = _signed(row.get("value"))
        if row.get("correctness") != "pass" or value is None or row.get("compatibility") is None:
            continue
        key = (str(row["task"]), str(row["arm"]), str(row["metric"]))
        groups.setdefault(key, {}).setdefault(row["compatibility"], []).append(value)
    medians: list[dict[str, object]] = []
    for (task, arm, metric), profiles in sorted(groups.items()):
        if len(profiles) != 1:
            continue  # unlike measurement profiles are never pooled
        [values] = profiles.values()
        medians.append(
            {
                "task": task,
                "arm": arm,
                "metric": metric,
                "value": statistics.median(values),
                "passing": len(values),
            }
        )
    by_key = {(row["task"], row["arm"], row["metric"]): float(str(row["value"])) for row in medians}
    aggregates: list[dict[str, object]] = []
    # Same task partition as the all-attempt aggregates: unlike analyzer scores are never averaged.
    for metric, group, tasks in _aggregate_groups(data):
        # Like the all-attempt overview, unresolved analyzer groups get no pooled value.
        unresolved = group in {"Unknown analyzer", "Conflicting analyzers"}
        for arm in arms:
            values = [
                by_key[(task, arm, metric)]
                for task in tasks
                if (task, arm, metric) in by_key and not unresolved
            ]
            aggregates.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "analysis_group": group,
                    "value": sum(values) / len(values) if values else None,
                    "measured_tasks": len(values),
                    "assigned_tasks": len(tasks),
                    "method": "passing attempts only; equal-weight mean of per-task medians",
                }
            )
    return medians, aggregates


def _cost_rows(
    observations: list[dict[str, object]], prices: dict[str, object]
) -> list[dict[str, object]]:
    """Coding dollars per attempt from a dated per-model price table; no table means unknown."""
    tokens: dict[str, dict[str, object]] = {}
    for row in observations:
        if row.get("family") == "tokens":
            tokens.setdefault(
                str(row["attempt_id"]),
                {"task": row["task"], "arm": row["arm"], "model": row["model"]},
            )[str(row["metric"])] = _number(row.get("value"))
    rows = []
    for attempt_id, values in sorted(tokens.items()):
        price = _map(prices.get(str(values["model"])))
        parts = [
            _number(values.get(name))
            for name in ("Uncached input tokens", "Cached input tokens", "Output tokens")
        ]
        rates = [
            _number(price.get(name))
            for name in ("input_per_million", "cached_input_per_million", "output_per_million")
        ]
        known_parts = [part for part in parts if part is not None]
        known_rates = [rate for rate in rates if rate is not None]
        dated = isinstance(price.get("as_of"), str) and bool(price.get("as_of"))
        known = len(known_parts) == len(known_rates) == 3 and dated
        rows.append(
            {
                "attempt_id": attempt_id,
                "task": values["task"],
                "arm": values["arm"],
                "model": values["model"],
                "dollars": sum(
                    part * rate for part, rate in zip(known_parts, known_rates, strict=True)
                )
                / 1_000_000
                if known
                else None,
                "price_as_of": price.get("as_of"),
                "reason": None
                if known
                else "no price for this model"
                if not price
                else "price table entry lacks an as_of date"
                if not dated
                else "price table entry lacks a rate"
                if len(known_rates) < 3
                else "token counts unavailable",
                "limitations": "uncached input is priced at the input rate; Claude cache writes, which cost more, are included there",
            }
        )
    return rows


def evaluation_data(report: dict[str, object], config: dict[str, object]) -> dict[str, object]:
    """Select measurements and derive baseline comparisons without scanning or model calls."""
    selected = config.get("measurements")
    if isinstance(selected, list) and "test_overlap" in selected:
        raise ValueError(REMOVED_TEST_OVERLAP)
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
        capability = _map(
            _map(_map(config.get("measurement_capabilities")).get(str(row.get("task")))).get(
                families[metric]
            )
        )
        if row.get("value") is None and capability.get("status") == "unavailable":
            row.update(status="unavailable", reason=capability.get("reason"))
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
        execution = _map(stage.get("execution_provenance"))
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
                "native_grading": stage.get("task_result"),
                "execution": {
                    **execution_outcome(execution),
                    **{
                        key: execution.get(key)
                        for key in (
                            "successful_native_completion",
                            "timed_out",
                            "exit_code",
                            "stream_errors",
                            "partial_usage",
                            "usage_accounting",
                            "actual_descendant_models",
                            "native_treatment",
                            "native_session_usage",
                            "error",
                            "attempt_status",
                        )
                    },
                },
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
            "Complexity counts what each submission added to source code: changed functions after the edit minus the same functions before. Unlike analyzers are reported separately; ratios compare each task with its reference solution."
        )
    if "coverage" in selected:
        limitations.append(
            "Coverage counts are proxies, not proof of correct assertions or unnecessary tests."
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
        "measurement_capabilities": config.get("measurement_capabilities", {}),
        "measurement_readiness": config.get("measurement_readiness", {}),
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
        "diagnostic_panels": sorted(DIAGNOSTIC_PANELS),
        "limitations": limitations,
    }
    data["comparisons"] = _comparison_rows(data)
    data["aggregates"] = _aggregate_rows(data)
    data["passing_medians"], data["passing_aggregates"] = _passing_summaries(data)
    data["reference_values"] = _reference_values(report)
    data["reference_comparisons"] = _reference_comparisons(data)
    data["reference_aggregates"] = _reference_aggregates(_rows(data["reference_comparisons"]))
    prices = _map(config.get("prices"))
    data["prices"] = prices or None
    data["cost"] = _cost_rows(observations, prices) if "tokens" in selected else []
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
