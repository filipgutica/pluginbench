from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pluginbench.results import ArmResult, canonical_fingerprint


def _divide(numerator: float | int | None, denominator: float | int) -> float | None:
    if numerator is None or denominator == 0:
        return None
    return float(numerator) / denominator


def _starting_patch_summary(arm: ArmResult) -> dict[str, Any] | None:
    results = [
        task.starting_patch for task in arm.tasks.values() if task.starting_patch is not None
    ]
    if not results:
        return None
    fields = (
        "repaired_attempts",
        "preserved_attempts",
        "regressed_attempts",
        "unchanged_failure_attempts",
        "unscored_attempts",
    )
    counts = {field: sum(getattr(result, field) for result in results) for field in fields}
    repair_total = counts["repaired_attempts"] + counts["unchanged_failure_attempts"]
    preservation_total = counts["preserved_attempts"] + counts["regressed_attempts"]
    return {
        "seeded_tasks": len(results),
        **counts,
        "repair_rate": _divide(counts["repaired_attempts"], repair_total),
        "preservation_rate": _divide(counts["preserved_attempts"], preservation_total),
    }


def _arm_summary(arm: ArmResult) -> dict[str, Any]:
    scoreable = sum(task.score is not None for task in arm.tasks.values())
    task_failures = sum(not task.passed and task.score is not None for task in arm.tasks.values())
    unscored = sum(task.score is None for task in arm.tasks.values())
    durations = [task.duration_seconds for task in arm.tasks.values()]
    duration_total = (
        sum(duration for duration in durations if duration is not None)
        if all(duration is not None for duration in durations)
        else None
    )
    return {
        "tasks_attempted": arm.tasks_attempted,
        "tasks_scoreable": scoreable,
        "tasks_passed": arm.tasks_passed,
        "task_failures": task_failures,
        "unscored_tasks": unscored,
        "pass_rate": _divide(arm.tasks_passed, arm.tasks_attempted),
        "input_tokens": arm.input_tokens,
        "cached_tokens": arm.cached_tokens,
        "output_tokens": arm.output_tokens,
        "tokens_per_task": _divide(
            (arm.input_tokens + arm.output_tokens)
            if arm.input_tokens is not None and arm.output_tokens is not None
            else None,
            arm.tasks_attempted,
        ),
        "total_cost_usd": arm.cost_usd,
        "known_cost_usd": arm.known_cost_usd,
        "cost_per_task_usd": _divide(arm.cost_usd, arm.tasks_attempted),
        "cost_per_successful_task_usd": _divide(arm.cost_usd, arm.tasks_passed),
        "duration_per_task_seconds": _divide(duration_total, arm.tasks_attempted),
        "infrastructure_errors": arm.infrastructure_errors,
        "tasks": {key: value.model_dump(mode="json") for key, value in sorted(arm.tasks.items())},
        "starting_patch": _starting_patch_summary(arm),
    }


def _paired_comparison(baseline: ArmResult, treatment: ArmResult) -> dict[str, Any]:
    common = sorted(baseline.tasks.keys() & treatment.tasks.keys())
    scoreable = [
        task
        for task in common
        if baseline.tasks[task].score is not None and treatment.tasks[task].score is not None
    ]
    score_pairs = [(baseline.tasks[task].score, treatment.tasks[task].score) for task in scoreable]
    numeric_pairs = [
        (baseline_score, treatment_score)
        for baseline_score, treatment_score in score_pairs
        if baseline_score is not None and treatment_score is not None
    ]
    baseline_wins = sum(
        baseline_score > treatment_score for baseline_score, treatment_score in numeric_pairs
    )
    treatment_wins = sum(
        treatment_score > baseline_score for baseline_score, treatment_score in numeric_pairs
    )
    unchanged = len(scoreable) - baseline_wins - treatment_wins
    score_lift = (
        sum(treatment_score - baseline_score for baseline_score, treatment_score in numeric_pairs)
        / len(numeric_pairs)
        if numeric_pairs
        else None
    )
    baseline_paired_passes = sum(baseline.tasks[task].passed for task in scoreable)
    treatment_paired_passes = sum(treatment.tasks[task].passed for task in scoreable)
    baseline_pass_rate = _divide(baseline_paired_passes, len(scoreable))
    treatment_pass_rate = _divide(treatment_paired_passes, len(scoreable))
    pass_rate_lift_pp = (
        (treatment_pass_rate - baseline_pass_rate) * 100
        if baseline_pass_rate is not None and treatment_pass_rate is not None
        else None
    )
    baseline_tokens = (
        baseline.input_tokens + baseline.output_tokens
        if baseline.input_tokens is not None and baseline.output_tokens is not None
        else None
    )
    treatment_tokens = (
        treatment.input_tokens + treatment.output_tokens
        if treatment.input_tokens is not None and treatment.output_tokens is not None
        else None
    )
    token_overhead = (
        treatment_tokens - baseline_tokens
        if baseline_tokens is not None and treatment_tokens is not None
        else None
    )
    cost_overhead = (
        treatment.cost_usd - baseline.cost_usd
        if baseline.cost_usd is not None and treatment.cost_usd is not None
        else None
    )
    baseline_cost = baseline.cost_usd
    cost_overhead_pct = (
        cost_overhead / baseline_cost * 100
        if cost_overhead is not None and baseline_cost is not None and baseline_cost != 0
        else None
    )
    additional_successes = treatment_paired_passes - baseline_paired_passes
    comparison = {
        "paired_tasks": len(scoreable),
        "unpaired_or_unscoreable_tasks": sorted(set(common) - set(scoreable)),
        "baseline_wins": baseline_wins,
        "treatment_wins": treatment_wins,
        "unchanged_tasks": unchanged,
        "score_lift": score_lift,
        "pass_rate_lift_pp": pass_rate_lift_pp,
        "token_overhead": token_overhead,
        "cost_overhead_usd": cost_overhead,
        "cost_overhead_pct": cost_overhead_pct,
        "incremental_cost_per_additional_success_usd": (
            cost_overhead / additional_successes
            if cost_overhead is not None and additional_successes > 0
            else None
        ),
    }
    baseline_starting = _starting_patch_summary(baseline)
    treatment_starting = _starting_patch_summary(treatment)
    if baseline_starting is not None and treatment_starting is not None:
        for metric in ("repair_rate", "preservation_rate"):
            baseline_rate = baseline_starting[metric]
            treatment_rate = treatment_starting[metric]
            comparison[f"{metric}_lift_pp"] = (
                (treatment_rate - baseline_rate) * 100
                if baseline_rate is not None and treatment_rate is not None
                else None
            )
    return comparison


def _decision(
    comparison: dict[str, Any],
    *,
    minimum_pass_rate_lift_pp: float,
    maximum_cost_overhead_pct: float | None,
    maximum_incremental_cost_per_additional_success_usd: float | None,
) -> dict[str, Any]:
    gates: dict[str, bool | None] = {
        "minimum_pass_rate_lift_pp": (
            comparison["pass_rate_lift_pp"] >= minimum_pass_rate_lift_pp
            if comparison["pass_rate_lift_pp"] is not None
            else None
        )
    }
    if (
        minimum_pass_rate_lift_pp == 0
        and maximum_cost_overhead_pct is None
        and maximum_incremental_cost_per_additional_success_usd is None
    ):
        return {"status": "not_configured", "gates": gates}
    if maximum_cost_overhead_pct is not None:
        gates["maximum_cost_overhead_pct"] = (
            comparison["cost_overhead_pct"] <= maximum_cost_overhead_pct
            if comparison["cost_overhead_pct"] is not None
            else None
        )
    if maximum_incremental_cost_per_additional_success_usd is not None:
        value = comparison["incremental_cost_per_additional_success_usd"]
        gates["maximum_incremental_cost_per_additional_success_usd"] = (
            value <= maximum_incremental_cost_per_additional_success_usd
            if value is not None
            else None
        )
    if comparison["unpaired_or_unscoreable_tasks"]:
        status = "insufficient_data"
    elif any(value is None for value in gates.values()):
        status = "insufficient_data"
    else:
        status = "justified" if all(gates.values()) else "not_justified"
    return {"status": status, "gates": gates}


def build_report(
    baseline: ArmResult | None,
    treatment: ArmResult,
    *,
    minimum_pass_rate_lift_pp: float = 0,
    maximum_cost_overhead_pct: float | None = None,
    maximum_incremental_cost_per_additional_success_usd: float | None = None,
    evaluate_decision: bool = True,
) -> dict[str, Any]:
    if treatment.arm != "treatment":
        raise ValueError("treatment result must have arm=treatment")
    if canonical_fingerprint(treatment.compatibility_payload) != (
        treatment.compatibility_fingerprint
    ):
        raise ValueError("treatment compatibility fingerprint is invalid")
    report: dict[str, Any] = {
        "schema_version": 1,
        "cost_source": "promptfoo_estimate",
        "treatment": _arm_summary(treatment),
    }
    if baseline is None:
        return report
    if baseline.arm != "baseline":
        raise ValueError("baseline result must have arm=baseline")
    if canonical_fingerprint(baseline.compatibility_payload) != baseline.compatibility_fingerprint:
        raise ValueError("baseline compatibility fingerprint is invalid")
    if baseline.compatibility_fingerprint != treatment.compatibility_fingerprint:
        raise ValueError("baseline and treatment compatibility fingerprints differ")
    if baseline.tasks.keys() != treatment.tasks.keys():
        raise ValueError("baseline and treatment task sets differ")
    if baseline.agent_versions != treatment.agent_versions:
        raise ValueError("baseline and treatment actual agent versions differ")
    if baseline.model_identifiers != treatment.model_identifiers:
        raise ValueError("baseline and treatment actual model identifiers differ")
    mismatched_checksums = [
        task
        for task in baseline.tasks
        if baseline.task_checksums.get(task) != treatment.task_checksums.get(task)
    ]
    if mismatched_checksums:
        raise ValueError(
            "baseline and treatment task checksums differ: " + ", ".join(mismatched_checksums)
        )
    report["baseline"] = _arm_summary(baseline)
    comparison = _paired_comparison(baseline, treatment)
    report["comparison"] = comparison
    if evaluate_decision:
        report["decision"] = _decision(
            comparison,
            minimum_pass_rate_lift_pp=minimum_pass_rate_lift_pp,
            maximum_cost_overhead_pct=maximum_cost_overhead_pct,
            maximum_incremental_cost_per_additional_success_usd=(
                maximum_incremental_cost_per_additional_success_usd
            ),
        )
    return report


def _display(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "Unavailable"
    if percent:
        return f"{float(value) * 100:.2f}%"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# pluginbench report", ""]
    for arm_name in ("baseline", "treatment"):
        arm = report.get(arm_name)
        if not isinstance(arm, dict):
            continue
        lines.extend(
            [
                f"## {arm_name.title()}",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Tasks attempted | {_display(arm['tasks_attempted'])} |",
                f"| Tasks passed | {_display(arm['tasks_passed'])} |",
                f"| Pass rate | {_display(arm['pass_rate'], percent=True)} |",
                f"| Task failures | {_display(arm['task_failures'])} |",
                f"| Unscored tasks | {_display(arm['unscored_tasks'])} |",
                f"| Input tokens | {_display(arm['input_tokens'])} |",
                f"| Cached tokens | {_display(arm['cached_tokens'])} |",
                f"| Output tokens | {_display(arm['output_tokens'])} |",
                f"| Tokens per task | {_display(arm['tokens_per_task'])} |",
                f"| Total cost USD | {_display(arm['total_cost_usd'])} |",
                f"| Cost per task USD | {_display(arm['cost_per_task_usd'])} |",
                "| Cost per successful task USD | "
                f"{_display(arm['cost_per_successful_task_usd'])} |",
                f"| Duration per task (s) | {_display(arm['duration_per_task_seconds'])} |",
                "| Infrastructure errors | "
                f"{_display(json.dumps(arm['infrastructure_errors'], sort_keys=True))} |",
                "",
            ]
        )
        starting_patch = arm.get("starting_patch")
        if isinstance(starting_patch, dict):
            lines.extend(
                [
                    "### Starting patch outcomes",
                    "",
                    "| Metric | Value |",
                    "| --- | ---: |",
                    f"| Seeded tasks | {_display(starting_patch['seeded_tasks'])} |",
                    f"| Repaired attempts | {_display(starting_patch['repaired_attempts'])} |",
                    f"| Preserved attempts | {_display(starting_patch['preserved_attempts'])} |",
                    f"| Regressed attempts | {_display(starting_patch['regressed_attempts'])} |",
                    "| Unchanged-failure attempts | "
                    f"{_display(starting_patch['unchanged_failure_attempts'])} |",
                    f"| Unscored attempts | {_display(starting_patch['unscored_attempts'])} |",
                    f"| Repair rate | {_display(starting_patch['repair_rate'], percent=True)} |",
                    "| Preservation rate | "
                    f"{_display(starting_patch['preservation_rate'], percent=True)} |",
                    "",
                ]
            )
    comparison = report.get("comparison")
    if isinstance(comparison, dict):
        comparison_lines = [
            "## Comparison",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Paired scoreable tasks | {_display(comparison['paired_tasks'])} |",
            "| Unpaired or unscoreable tasks | "
            f"{_display(', '.join(comparison['unpaired_or_unscoreable_tasks']))} |",
            f"| Baseline wins | {_display(comparison['baseline_wins'])} |",
            f"| Treatment wins | {_display(comparison['treatment_wins'])} |",
            f"| Unchanged tasks | {_display(comparison['unchanged_tasks'])} |",
            f"| Score lift | {_display(comparison['score_lift'])} |",
            f"| Pass-rate lift (pp) | {_display(comparison['pass_rate_lift_pp'])} |",
            f"| Token overhead | {_display(comparison['token_overhead'])} |",
            f"| Cost overhead USD | {_display(comparison['cost_overhead_usd'])} |",
            f"| Cost overhead (%) | {_display(comparison['cost_overhead_pct'])} |",
            "| Incremental cost per additional success USD | "
            f"{_display(comparison['incremental_cost_per_additional_success_usd'])} |",
            "",
        ]
        if "repair_rate_lift_pp" in comparison:
            comparison_lines.insert(
                -1,
                f"| Repair rate lift (pp) | {_display(comparison['repair_rate_lift_pp'])} |",
            )
            comparison_lines.insert(
                -1,
                "| Preservation rate lift (pp) | "
                f"{_display(comparison['preservation_rate_lift_pp'])} |",
            )
        decision = report.get("decision")
        if isinstance(decision, dict):
            comparison_lines.extend([f"Decision: **{decision['status']}**", ""])
        lines.extend(comparison_lines)
    return "\n".join(lines)


def write_reports(run_dir: Path, report: dict[str, Any]) -> None:
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (run_dir / "report.md").write_text(render_markdown(report))
