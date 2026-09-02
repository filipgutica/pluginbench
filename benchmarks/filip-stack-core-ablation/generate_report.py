from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pluginbench.results import ArmResult, TaskResult, canonical_fingerprint, load_arm


def _sum_complete(values: list[int | float | None]) -> int | float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _summary(arm: ArmResult, task_ids: list[str]) -> dict[str, Any]:
    tasks = [arm.tasks[task_id] for task_id in task_ids]
    input_tokens = _sum_complete([task.usage.input_tokens for task in tasks])
    cached_tokens = _sum_complete([task.usage.cached_tokens for task in tasks])
    output_tokens = _sum_complete([task.usage.output_tokens for task in tasks])
    cost = _sum_complete([task.usage.cost_usd for task in tasks])
    duration = _sum_complete([task.duration_seconds for task in tasks])
    total_tokens = (
        input_tokens + output_tokens
        if isinstance(input_tokens, int) and isinstance(output_tokens, int)
        else None
    )
    passed = sum(task.passed for task in tasks)
    return {
        "tasks": len(tasks),
        "passed": passed,
        "pass_rate": passed / len(tasks),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tokens_per_task": total_tokens / len(tasks) if total_tokens is not None else None,
        "total_cost_usd": cost,
        "cost_per_task_usd": cost / len(tasks) if isinstance(cost, int | float) else None,
        "duration_per_task_seconds": (
            duration / len(tasks) if isinstance(duration, int | float) else None
        ),
    }


def _comparison(
    reference: ArmResult,
    candidate: ArmResult,
    task_ids: list[str],
    reference_summary: dict[str, Any],
    candidate_summary: dict[str, Any],
) -> dict[str, Any]:
    pairs: list[tuple[TaskResult, TaskResult]] = [
        (reference.tasks[task_id], candidate.tasks[task_id]) for task_id in task_ids
    ]
    score_pairs: list[tuple[float, float]] = []
    for reference_task, candidate_task in pairs:
        assert reference_task.score is not None
        assert candidate_task.score is not None
        score_pairs.append((reference_task.score, candidate_task.score))
    candidate_wins = sum(
        candidate_score > reference_score for reference_score, candidate_score in score_pairs
    )
    reference_wins = sum(
        reference_score > candidate_score for reference_score, candidate_score in score_pairs
    )
    token_overhead = candidate_summary["total_tokens"] - reference_summary["total_tokens"]
    cost_overhead = candidate_summary["total_cost_usd"] - reference_summary["total_cost_usd"]
    duration_overhead = (
        candidate_summary["duration_per_task_seconds"]
        - reference_summary["duration_per_task_seconds"]
    )
    return {
        "reference_wins": reference_wins,
        "candidate_wins": candidate_wins,
        "unchanged": len(task_ids) - reference_wins - candidate_wins,
        "pass_rate_lift_pp": (candidate_summary["pass_rate"] - reference_summary["pass_rate"])
        * 100,
        "token_overhead": token_overhead,
        "token_overhead_pct": token_overhead / reference_summary["total_tokens"] * 100,
        "cost_overhead_usd": cost_overhead,
        "cost_overhead_pct": cost_overhead / reference_summary["total_cost_usd"] * 100,
        "duration_overhead_per_task_seconds": duration_overhead,
    }


def _display(value: Any, *, digits: int = 4) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _currency(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.4f}"


def _markdown(report: dict[str, Any]) -> str:
    summaries = report["summaries"]
    lines = [
        "# Filip-stack core ablation report",
        "",
        (
            f"This report compares the {len(report['comparable_task_ids'])} tasks scoreable "
            "in the baseline, full Filip-stack, and core-skill arms."
        ),
        "",
        "| Metric | Baseline | Full bundle | Core bundle |",
        "| --- | ---: | ---: | ---: |",
    ]
    rows = (
        ("Tasks passed", "passed", 0),
        ("Pass rate", "pass_rate", 4),
        ("Total tokens", "total_tokens", 0),
        ("Tokens per task", "tokens_per_task", 0),
        ("Nominal cost USD", "total_cost_usd", 4),
        ("Cost per task USD", "cost_per_task_usd", 4),
        ("Duration per task (s)", "duration_per_task_seconds", 1),
    )
    for label, field, digits in rows:
        values = [summaries[name][field] for name in ("baseline", "full", "core")]
        displayed = (
            [f"{value * 100:.2f}%" for value in values]
            if field == "pass_rate"
            else [_display(value, digits=digits) for value in values]
        )
        lines.append(f"| {label} | {displayed[0]} | {displayed[1]} | {displayed[2]} |")
    lines.extend(["", "## Comparisons", ""])
    for label, key in (("Core vs baseline", "baseline_to_core"), ("Core vs full", "full_to_core")):
        comparison = report["comparisons"][key]
        lines.extend(
            [
                f"### {label}",
                "",
                f"- Reference wins: {comparison['reference_wins']}",
                f"- Core wins: {comparison['candidate_wins']}",
                f"- Unchanged: {comparison['unchanged']}",
                f"- Pass-rate lift: {_display(comparison['pass_rate_lift_pp'], digits=2)} percentage points",
                f"- Token overhead: {_display(comparison['token_overhead'], digits=0)} ({_display(comparison['token_overhead_pct'], digits=2)}%)",
                f"- Nominal cost overhead: {_currency(comparison['cost_overhead_usd'])} "
                f"({_display(comparison['cost_overhead_pct'], digits=2)}%)",
                f"- Duration overhead: {_display(comparison['duration_overhead_per_task_seconds'], digits=1)} seconds per task",
                "",
            ]
        )
    lines.extend(
        [
            "## Task outcomes",
            "",
            "| Task | Baseline | Full bundle | Core bundle |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for task_id, outcome in report["task_outcomes"].items():
        lines.append(
            f"| `{task_id}` | {outcome['baseline']} | {outcome['full']} | {outcome['core']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-arm", type=Path, required=True)
    parser.add_argument("--full-arm", type=Path, required=True)
    parser.add_argument("--core-arm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arms = {
        "baseline": load_arm(arguments.baseline_arm),
        "full": load_arm(arguments.full_arm),
        "core": load_arm(arguments.core_arm),
    }
    if arms["baseline"].arm != "baseline":
        raise ValueError("--baseline-arm must contain a baseline arm")
    if arms["full"].arm != "treatment" or arms["core"].arm != "treatment":
        raise ValueError("--full-arm and --core-arm must contain treatment arms")
    invalid_fingerprints = [
        name
        for name, arm in arms.items()
        if canonical_fingerprint(arm.compatibility_payload) != arm.compatibility_fingerprint
    ]
    if invalid_fingerprints:
        raise ValueError(
            "arm compatibility payload does not match its fingerprint: "
            + ", ".join(invalid_fingerprints)
        )
    fingerprints = {arm.compatibility_fingerprint for arm in arms.values()}
    if len(fingerprints) != 1:
        raise ValueError("arms have incompatible baseline fingerprints")
    common = set.intersection(*(set(arm.tasks) for arm in arms.values()))
    comparable = sorted(
        task_id
        for task_id in common
        if all(arm.tasks[task_id].score is not None for arm in arms.values())
    )
    if not comparable:
        raise ValueError("arms have no mutually scoreable tasks")
    summaries = {name: _summary(arm, comparable) for name, arm in arms.items()}
    report = {
        "schema_version": 1,
        "sources": {
            "baseline_arm": str(arguments.baseline_arm.resolve()),
            "full_arm": str(arguments.full_arm.resolve()),
            "core_arm": str(arguments.core_arm.resolve()),
            "compatibility_fingerprint": fingerprints.pop(),
        },
        "comparable_task_ids": comparable,
        "excluded_task_ids": sorted(common - set(comparable)),
        "summaries": summaries,
        "comparisons": {
            "baseline_to_core": _comparison(
                arms["baseline"],
                arms["core"],
                comparable,
                summaries["baseline"],
                summaries["core"],
            ),
            "full_to_core": _comparison(
                arms["full"],
                arms["core"],
                comparable,
                summaries["full"],
                summaries["core"],
            ),
        },
        "task_outcomes": {
            task_id: {
                name: "pass" if arm.tasks[task_id].passed else "fail" for name, arm in arms.items()
            }
            for task_id in comparable
        },
    }
    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "ablation-report.json").write_text(json.dumps(report, indent=2) + "\n")
    (arguments.output / "ablation-report.md").write_text(_markdown(report))


if __name__ == "__main__":
    main()
