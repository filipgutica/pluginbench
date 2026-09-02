from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from pluginbench.review_models import ReviewAttemptResult


def _complete_sum(values: Iterable[int | float | None]) -> int | float | None:
    items = list(values)
    if not items or any(value is None for value in items):
        return None
    return sum(value for value in items if value is not None)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _candidate_review_summary(
    candidate_ids: list[str],
    attempts: list[ReviewAttemptResult],
    expected_attempts: int,
) -> dict[str, Any]:
    selected = [attempt for attempt in attempts if attempt.candidate_id in candidate_ids]
    valid = [attempt for attempt in selected if attempt.response is not None]
    accepts = sum(
        attempt.response is not None and attempt.response.verdict == "accept" for attempt in valid
    )
    changes = len(valid) - accepts
    findings = [
        finding
        for attempt in valid
        for finding in (attempt.response.findings if attempt.response is not None else [])
    ]
    severity = Counter(finding.severity for finding in findings)
    category = Counter(finding.category.value for finding in findings)
    by_candidate: dict[str, list[ReviewAttemptResult]] = defaultdict(list)
    for attempt in valid:
        by_candidate[attempt.candidate_id].append(attempt)
    complete_candidates = [
        candidate_id
        for candidate_id in candidate_ids
        if len(by_candidate[candidate_id]) == expected_attempts
    ]
    agreements = sum(
        len(
            {attempt.response.verdict for attempt in by_candidate[candidate_id] if attempt.response}
        )
        == 1
        for candidate_id in complete_candidates
    )
    incomplete = [
        candidate_id
        for candidate_id in candidate_ids
        if len(by_candidate[candidate_id]) != expected_attempts
    ]
    durations = [
        attempt.duration_seconds for attempt in valid if attempt.duration_seconds is not None
    ]
    return {
        "candidates": len(candidate_ids),
        "expected_reviews": len(candidate_ids) * expected_attempts,
        "valid_reviews": len(valid),
        "infrastructure_failures": len(selected) - len(valid),
        "accept_count": accepts,
        "accept_rate": _rate(accepts, len(valid)),
        "changes_required_count": changes,
        "changes_required_rate": _rate(changes, len(valid)),
        "findings": len(findings),
        "findings_per_review": _rate(len(findings), len(valid)),
        "findings_by_severity": {
            "blocker": severity["blocker"],
            "major": severity["major"],
            "minor": severity["minor"],
        },
        "findings_by_category": dict(sorted(category.items())),
        "mean_review_duration_seconds": mean(durations) if durations else None,
        "reviewer_usage": {
            "input_tokens": _complete_sum(attempt.usage.input_tokens for attempt in selected),
            "cached_tokens": _complete_sum(attempt.usage.cached_tokens for attempt in selected),
            "uncached_input_tokens": _complete_sum(
                attempt.usage.uncached_input_tokens for attempt in selected
            ),
            "output_tokens": _complete_sum(attempt.usage.output_tokens for attempt in selected),
            "cost_usd": _complete_sum(attempt.usage.cost_usd for attempt in selected),
        },
        "verdict_agreement_rate": _rate(agreements, len(complete_candidates)),
        "complete_candidates_for_agreement": len(complete_candidates),
        "incomplete_candidates": incomplete,
    }


def _attempts_from_directory(run_directory: Path) -> tuple[ReviewAttemptResult, ...]:
    attempts: list[ReviewAttemptResult] = []
    for path in sorted((run_directory / "reviews").glob("*/attempt-*/result.json")):
        attempt = ReviewAttemptResult.model_validate_json(path.read_text(encoding="utf-8"))
        candidate_id = path.parent.parent.name
        match = re.fullmatch(r"attempt-(\d+)", path.parent.name)
        if (
            match is None
            or attempt.candidate_id != candidate_id
            or attempt.attempt != int(match.group(1))
        ):
            raise ValueError(f"review result {path} does not match its artifact path")
        attempts.append(attempt)
    return tuple(attempts)


def _validate_candidate_map(
    candidate_map: Any, manifest: dict[str, Any]
) -> dict[str, dict[str, str]]:
    if not isinstance(candidate_map, dict):
        raise ValueError("candidate map must be a JSON object")
    manifest_candidates = manifest.get("candidates")
    if not isinstance(manifest_candidates, dict):
        raise ValueError("review manifest candidates must be a JSON object")
    if set(candidate_map) != set(manifest_candidates):
        raise ValueError("candidate map and review manifest candidates do not match")
    validated: dict[str, dict[str, str]] = {}
    identities: set[tuple[str, str]] = set()
    for candidate_id, item in candidate_map.items():
        if not isinstance(candidate_id, str) or not isinstance(item, dict):
            raise ValueError("candidate map entries must contain candidate objects")
        task_id = item.get("task_id")
        source_label = item.get("source_label")
        if not isinstance(task_id, str) or source_label not in {
            "baseline",
            "full",
            "core",
            "gold",
        }:
            raise ValueError(f"candidate map entry is invalid: {candidate_id}")
        identity = (task_id, source_label)
        if identity in identities:
            raise ValueError(
                f"duplicate candidate mapping for task {task_id} and arm {source_label}"
            )
        identities.add(identity)
        validated[candidate_id] = {"task_id": task_id, "source_label": source_label}
    return validated


def _validate_attempts(
    attempts: list[ReviewAttemptResult], candidate_ids: set[str], expected_attempts: int
) -> None:
    identities: set[tuple[str, int]] = set()
    for attempt in attempts:
        if attempt.candidate_id not in candidate_ids:
            raise ValueError(f"review result references unknown candidate: {attempt.candidate_id}")
        if attempt.attempt > expected_attempts:
            raise ValueError(
                f"review attempt {attempt.attempt} is outside the configured range for "
                f"{attempt.candidate_id}"
            )
        identity = (attempt.candidate_id, attempt.attempt)
        if identity in identities:
            raise ValueError(
                f"duplicate review attempt {attempt.attempt} for {attempt.candidate_id}"
            )
        identities.add(identity)


def _gold_calibration_assessment(
    gold: dict[str, Any], review_arms: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    incomplete_groups = [
        label
        for label, summary in {"gold": gold, **review_arms}.items()
        if summary["incomplete_candidates"]
    ]
    if incomplete_groups:
        return {
            "status": "insufficient_data",
            "warnings": [
                "Gold calibration assessment was withheld because review attempts are "
                "incomplete for: " + ", ".join(incomplete_groups) + "."
            ],
            "arm_accept_rates": {
                label: summary["accept_rate"] for label, summary in review_arms.items()
            },
            "arm_findings_per_review": {
                label: summary["findings_per_review"] for label, summary in review_arms.items()
            },
            "interpretation": "Incomplete review samples cannot calibrate reviewer behavior.",
        }
    gold_accept_rate = gold["accept_rate"]
    gold_findings_per_review = gold["findings_per_review"]
    arm_accept_rates = {label: summary["accept_rate"] for label, summary in review_arms.items()}
    arm_findings_per_review = {
        label: summary["findings_per_review"] for label, summary in review_arms.items()
    }
    warnings: list[str] = []
    valid_accept_rates = [value for value in arm_accept_rates.values() if value is not None]
    if (
        gold_accept_rate is not None
        and len(valid_accept_rates) == len(arm_accept_rates)
        and gold_accept_rate < min(valid_accept_rates)
    ):
        warnings.append(
            "Gold patches received a lower reviewer acceptance rate than every experimental arm."
        )
    valid_finding_rates = [value for value in arm_findings_per_review.values() if value is not None]
    if (
        gold_findings_per_review is not None
        and len(valid_finding_rates) == len(arm_findings_per_review)
        and gold_findings_per_review > max(valid_finding_rates)
    ):
        warnings.append(
            "Gold patches received more findings per review than every experimental arm."
        )
    return {
        "status": "warning" if warnings else "no_cross_arm_warning",
        "warnings": warnings,
        "arm_accept_rates": arm_accept_rates,
        "arm_findings_per_review": arm_findings_per_review,
        "interpretation": (
            "Do not treat raw reviewer verdicts or findings as validated quality evidence "
            "without adjudication."
            if warnings
            else "Gold calibration did not trigger a cross-arm warning."
        ),
    }


def _appended_gold_calibration(
    run_directory: Path, source_manifest: dict[str, Any]
) -> dict[str, Any] | None:
    calibration = run_directory / "gold-calibration"
    if not calibration.exists():
        return None
    try:
        calibration_manifest = json.loads(
            (calibration / "manifest.json").read_text(encoding="utf-8")
        )
        candidate_map = json.loads(
            (calibration / "private" / "candidate-map.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read appended gold calibration: {exc}") from exc
    if not isinstance(calibration_manifest, dict) or not isinstance(candidate_map, dict):
        raise ValueError("appended gold calibration metadata must contain JSON objects")
    candidate_map = _validate_candidate_map(candidate_map, calibration_manifest)
    source_digest = (
        "sha256:" + hashlib.sha256((run_directory / "manifest.json").read_bytes()).hexdigest()
    )
    if calibration_manifest.get("source_review_manifest_digest") != source_digest:
        raise ValueError("appended gold calibration does not match the source review manifest")
    if calibration_manifest.get("compatibility_fingerprint") != source_manifest.get(
        "compatibility_fingerprint"
    ):
        raise ValueError("appended gold calibration fingerprint does not match")
    source_tasks = {
        str(item.get("task_id")): str(item.get("digest"))
        for item in source_manifest.get("tasks", [])
        if isinstance(item, dict)
    }
    calibration_tasks = {
        str(item.get("task_id")): str(item.get("digest"))
        for item in calibration_manifest.get("tasks", [])
        if isinstance(item, dict)
    }
    if calibration_tasks != source_tasks:
        raise ValueError("appended gold calibration tasks do not match")
    if calibration_manifest.get("reviewer") != source_manifest.get("reviewer"):
        raise ValueError("appended gold calibration reviewer does not match")
    if calibration_manifest.get("expected_attempts_per_candidate") != source_manifest.get(
        "expected_attempts_per_candidate"
    ):
        raise ValueError("appended gold calibration attempt count does not match")
    gold_by_task = {
        str(item.get("task_id")): candidate_id
        for candidate_id, item in candidate_map.items()
        if isinstance(item, dict) and item.get("source_label") == "gold"
    }
    if set(gold_by_task) != set(source_tasks):
        raise ValueError("appended gold calibration must contain one candidate per source task")
    attempts = list(_attempts_from_directory(calibration))
    expected_attempts = int(calibration_manifest["expected_attempts_per_candidate"])
    _validate_attempts(attempts, set(candidate_map), expected_attempts)
    summary = _candidate_review_summary(sorted(gold_by_task.values()), attempts, expected_attempts)
    summary["per_task"] = [
        {
            "task_id": task_id,
            "review": _task_review_result(gold_by_task[task_id], attempts, expected_attempts),
        }
        for task_id in source_tasks
    ]
    summary["provenance"] = {
        "started_at": calibration_manifest.get("started_at"),
        "finished_at": calibration_manifest.get("finished_at"),
        "run_directory": str(calibration.resolve()),
    }
    return summary


def _implementation_summary(tasks: list[dict[str, Any]], label: str) -> dict[str, Any]:
    results = [task["implementation_results"][label] for task in tasks]
    input_tokens = _complete_sum(result["usage"]["input_tokens"] for result in results)
    cached_tokens = _complete_sum(result["usage"]["cached_tokens"] for result in results)
    uncached = (
        input_tokens - cached_tokens
        if isinstance(input_tokens, int) and isinstance(cached_tokens, int)
        else None
    )
    return {
        "tasks_attempted": len(results),
        "tasks_passed": sum(bool(result["passed"]) for result in results),
        "pass_rate": _rate(sum(bool(result["passed"]) for result in results), len(results)),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "uncached_input_tokens": uncached,
        "output_tokens": _complete_sum(result["usage"]["output_tokens"] for result in results),
        "nominal_cost_usd": _complete_sum(result["usage"]["cost_usd"] for result in results),
        "duration_seconds": _complete_sum(result["duration_seconds"] for result in results),
        "infrastructure_errors": sum(len(result["infrastructure_errors"]) for result in results),
    }


def _task_review_result(
    candidate_id: str,
    attempts: list[ReviewAttemptResult],
    expected_attempts: int,
) -> dict[str, Any]:
    summary = _candidate_review_summary([candidate_id], attempts, expected_attempts)
    summary["attempts"] = [
        {
            "attempt": attempt.attempt,
            "verdict": attempt.response.verdict.value if attempt.response is not None else None,
            "findings": (
                [finding.model_dump(mode="json") for finding in attempt.response.findings]
                if attempt.response is not None
                else []
            ),
            "infrastructure_errors": attempt.infrastructure_errors,
        }
        for attempt in sorted(
            (item for item in attempts if item.candidate_id == candidate_id),
            key=lambda item: item.attempt,
        )
    ]
    return summary


def _paired_differences(per_task: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = (("full", "baseline"), ("core", "baseline"), ("full", "core"))
    output: list[dict[str, Any]] = []
    for left, right in comparisons:
        task_differences: list[dict[str, Any]] = []
        for task in per_task:
            left_review = task["reviews"][left]
            right_review = task["reviews"][right]
            complete = (
                not left_review["incomplete_candidates"]
                and not right_review["incomplete_candidates"]
            )
            left_rate = left_review["changes_required_rate"] if complete else None
            right_rate = right_review["changes_required_rate"] if complete else None
            left_findings = left_review["findings_per_review"] if complete else None
            right_findings = right_review["findings_per_review"] if complete else None
            task_differences.append(
                {
                    "task_id": task["task_id"],
                    "changes_required_rate_difference": (
                        left_rate - right_rate
                        if left_rate is not None and right_rate is not None
                        else None
                    ),
                    "findings_per_review_difference": (
                        left_findings - right_findings
                        if left_findings is not None and right_findings is not None
                        else None
                    ),
                }
            )
        complete_change = [
            item["changes_required_rate_difference"]
            for item in task_differences
            if item["changes_required_rate_difference"] is not None
        ]
        complete_findings = [
            item["findings_per_review_difference"]
            for item in task_differences
            if item["findings_per_review_difference"] is not None
        ]
        output.append(
            {
                "comparison": f"{left}-minus-{right}",
                "mean_task_paired_changes_required_rate_difference": (
                    mean(complete_change) if complete_change else None
                ),
                "mean_task_paired_findings_per_review_difference": (
                    mean(complete_findings) if complete_findings else None
                ),
                "tasks": task_differences,
            }
        )
    return output


def build_review_report(
    run_directory: Path,
    *,
    attempts: tuple[ReviewAttemptResult, ...] | None = None,
) -> dict[str, Any]:
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    candidate_map = json.loads(
        (run_directory / "private" / "candidate-map.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest, dict):
        raise ValueError("review manifest must be a JSON object")
    candidate_map = _validate_candidate_map(candidate_map, manifest)
    loaded_attempts = list(
        attempts if attempts is not None else _attempts_from_directory(run_directory)
    )
    expected_attempts = int(manifest["expected_attempts_per_candidate"])
    _validate_attempts(loaded_attempts, set(candidate_map), expected_attempts)
    by_label = {
        label: sorted(
            candidate_id
            for candidate_id, item in candidate_map.items()
            if item["source_label"] == label
        )
        for label in ("baseline", "full", "core", "gold")
    }
    review_arms = {
        label: _candidate_review_summary(by_label[label], loaded_attempts, expected_attempts)
        for label in ("baseline", "full", "core")
    }
    tasks = manifest["tasks"]
    implementation = {
        label: _implementation_summary(tasks, label) for label in ("baseline", "full", "core")
    }
    candidate_by_task_label = {
        (item["task_id"], item["source_label"]): candidate_id
        for candidate_id, item in candidate_map.items()
    }
    per_task: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task["task_id"]
        reviews: dict[str, Any] = {}
        patch_stats: dict[str, Any] = {}
        for label in ("baseline", "full", "core"):
            candidate_id = candidate_by_task_label[(task_id, label)]
            reviews[label] = _task_review_result(candidate_id, loaded_attempts, expected_attempts)
            patch_stats[label] = manifest["candidates"][candidate_id]
        signals = {}
        for label in ("baseline", "full", "core"):
            findings = [
                finding for attempt in reviews[label]["attempts"] for finding in attempt["findings"]
            ]
            passed = bool(task["implementation_results"][label]["passed"])
            complete = not reviews[label]["incomplete_candidates"]
            signals[label] = {
                "verifier_failure_without_compatibility_finding": (
                    (
                        not passed
                        and not any(finding["category"] == "compatibility" for finding in findings)
                    )
                    if complete
                    else None
                ),
                "passing_patch_received_change_request": (
                    (passed and reviews[label]["changes_required_count"] > 0) if complete else None
                ),
            }
        per_task.append(
            {
                "task_id": task_id,
                "implementation": task["implementation_results"],
                "patches": patch_stats,
                "reviews": reviews,
                "signals": signals,
            }
        )

    expected_total = len(candidate_map) * expected_attempts
    warnings: list[str] = []
    stop_path = run_directory / "execution-stop.json"
    if stop_path.is_file():
        stop = json.loads(stop_path.read_text(encoding="utf-8"))
        warnings.append(
            f"Execution stopped after {stop['completed_review_calls']} review calls: "
            f"{stop['reason']}."
        )
    if len(loaded_attempts) != expected_total:
        warnings.append(
            f"Incomplete review data: expected {expected_total} attempts, found {len(loaded_attempts)}."
        )
    for label, summary in review_arms.items():
        if summary["incomplete_candidates"]:
            warnings.append(
                f"{label} has incomplete candidates; missing data is not treated as acceptance."
            )
    infrastructure_errors = [
        {
            "candidate_id": attempt.candidate_id,
            "attempt": attempt.attempt,
            "errors": attempt.infrastructure_errors,
        }
        for attempt in loaded_attempts
        if attempt.infrastructure_errors
    ]
    gold = (
        _candidate_review_summary(by_label["gold"], loaded_attempts, expected_attempts)
        if by_label["gold"]
        else _appended_gold_calibration(run_directory, manifest)
    )
    if gold is not None and gold["incomplete_candidates"]:
        warnings.append(
            "Gold calibration has incomplete candidates; missing data is not treated as acceptance."
        )
    if gold is not None:
        gold["calibration_assessment"] = _gold_calibration_assessment(gold, review_arms)
        warnings.extend(gold["calibration_assessment"]["warnings"])
    return {
        "schema_version": 1,
        "title": "PluginBench blinded reviewer-proxy evaluation",
        "study_name": manifest["study_name"],
        "provenance": {
            "compatibility_fingerprint": manifest["compatibility_fingerprint"],
            "dataset": manifest["dataset"],
            "source_arms": manifest["source_arms"],
            "source_arm_provenance": manifest["source_arm_provenance"],
            "reviewer": manifest["reviewer"],
            "review_tooling": manifest["review_tooling"],
            "started_at": manifest["started_at"],
            "finished_at": manifest["finished_at"],
            "task_ids": [task["task_id"] for task in tasks],
            "task_checksums": {task["task_id"]: task["digest"] for task in tasks},
            "excluded_tasks": manifest["excluded_tasks"],
        },
        "capability_results": implementation,
        "reviewer_proxy_results": review_arms,
        "paired_review_differences": _paired_differences(per_task),
        "per_task": per_task,
        "resource_overhead": {
            "implementation": implementation,
            "reviewer": {
                label: summary["reviewer_usage"] for label, summary in review_arms.items()
            },
        },
        "gold_patch_calibration": gold,
        "infrastructure_failures": infrastructure_errors,
        "warnings": warnings,
        "evidence_limitations": manifest["limitations"],
    }


def _display(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "missing"
    if percent:
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_review_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['title']}",
        "",
        "> These are automated reviewer-proxy results. They do not measure actual human review or correction time.",
        "",
        "## 1. Existing capability results",
        "",
        "| Arm | Passed | Attempted | Pass rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, item in report["capability_results"].items():
        lines.append(
            f"| {label} | {item['tasks_passed']} | {item['tasks_attempted']} | "
            f"{_display(item['pass_rate'], percent=True)} |"
        )
    lines.extend(
        [
            "",
            "## 2. Reviewer-proxy findings",
            "",
            "| Arm | Accept rate | Changes required | Findings/review | Blocker | Major | Minor | Agreement |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, item in report["reviewer_proxy_results"].items():
        severity = item["findings_by_severity"]
        lines.append(
            f"| {label} | {_display(item['accept_rate'], percent=True)} | "
            f"{_display(item['changes_required_rate'], percent=True)} | "
            f"{_display(item['findings_per_review'])} | {severity['blocker']} | "
            f"{severity['major']} | {severity['minor']} | "
            f"{_display(item['verdict_agreement_rate'], percent=True)} |"
        )
    lines.extend(["", "Finding categories:", ""])
    for label, item in report["reviewer_proxy_results"].items():
        categories = item["findings_by_category"]
        rendered = ", ".join(f"{name}={count}" for name, count in categories.items())
        lines.append(f"- {label}: {rendered or 'none'}")
    lines.extend(
        [
            "",
            "Task-paired outcomes:",
            "",
            "| Task | Capability B/F/C | Accept rate B/F/C | Findings/review B/F/C |",
            "| --- | --- | --- | --- |",
        ]
    )
    for task in report["per_task"]:
        implementation = task["implementation"]
        reviews = task["reviews"]
        capability = "/".join(
            "pass" if implementation[label]["passed"] else "fail"
            for label in ("baseline", "full", "core")
        )
        accept_rates = "/".join(
            _display(reviews[label]["accept_rate"], percent=True)
            for label in ("baseline", "full", "core")
        )
        findings = "/".join(
            _display(reviews[label]["findings_per_review"])
            for label in ("baseline", "full", "core")
        )
        lines.append(f"| `{task['task_id']}` | {capability} | {accept_rates} | {findings} |")
    lines.extend(["", "Verifier-versus-review discrepancies:", ""])
    discrepancy_count = 0
    for task in report["per_task"]:
        for label, signals in task["signals"].items():
            if signals["verifier_failure_without_compatibility_finding"]:
                lines.append(
                    f"- `{task['task_id']}` {label}: the saved verifier failed, but reviewers "
                    "reported no compatibility finding. The relevant risk may have been missed."
                )
                discrepancy_count += 1
            if signals["passing_patch_received_change_request"]:
                lines.append(
                    f"- `{task['task_id']}` {label}: the saved verifier passed, but at least "
                    "one reviewer requested changes."
                )
                discrepancy_count += 1
    if discrepancy_count == 0:
        lines.append("None recorded.")
    lines.extend(["", "Action-required finding evidence:", ""])
    finding_count = 0
    for task in report["per_task"]:
        for label, review in task["reviews"].items():
            for attempt in review["attempts"]:
                for finding in attempt["findings"]:
                    lines.append(
                        f"- `{task['task_id']}` {label} attempt {attempt['attempt']}, "
                        f"{finding['severity']} {finding['category']} at "
                        f"`{finding['file']}:{finding['line']}`: {finding['evidence']} "
                        f"Required action: {finding['required_action']}"
                    )
                    finding_count += 1
    if finding_count == 0:
        lines.append("None recorded.")
    lines.extend(
        [
            "",
            "Paired differences are reported in `review-report.json`; no composite quality score is calculated.",
            "",
            "## 3. Resource overhead",
            "",
            "| Arm | Implementation input | Cached | Uncached | Output | Nominal cost | Duration (s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, item in report["resource_overhead"]["implementation"].items():
        lines.append(
            f"| {label} | {_display(item['input_tokens'])} | {_display(item['cached_tokens'])} | "
            f"{_display(item['uncached_input_tokens'])} | {_display(item['output_tokens'])} | "
            f"{_display(item['nominal_cost_usd'])} | {_display(item['duration_seconds'])} |"
        )
    lines.extend(
        [
            "",
            "Reviewer resource use:",
            "",
            "| Arm | Input | Cached | Uncached | Output | Cost | Mean duration (s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, item in report["resource_overhead"]["reviewer"].items():
        lines.append(
            f"| {label} | {_display(item['input_tokens'])} | "
            f"{_display(item['cached_tokens'])} | {_display(item['uncached_input_tokens'])} | "
            f"{_display(item['output_tokens'])} | {_display(item['cost_usd'])} | "
            f"{_display(report['reviewer_proxy_results'][label]['mean_review_duration_seconds'])} |"
        )
    lines.extend(["", "## 4. Gold-patch calibration", ""])
    gold = report["gold_patch_calibration"]
    if gold is None:
        lines.append("Gold calibration was disabled.")
    else:
        lines.append(
            f"Gold candidates: {gold['candidates']}. Accept rate: "
            f"{_display(gold['accept_rate'], percent=True)}. "
            f"Action-required findings: {gold['findings']}."
        )
        usage = gold["reviewer_usage"]
        lines.append(
            "Gold reviewer resources: "
            f"{_display(usage['input_tokens'])} input tokens. "
            f"Cached input: {_display(usage['cached_tokens'])}. "
            f"Output: {_display(usage['output_tokens'])}. "
            f"Nominal cost: {_display(usage['cost_usd'])}. "
            f"{_display(gold['mean_review_duration_seconds'])} mean duration seconds."
        )
        assessment = gold["calibration_assessment"]
        if assessment["warnings"]:
            lines.extend(["", "Calibration warnings:", ""])
            lines.extend(f"- {warning}" for warning in assessment["warnings"])
            lines.append(f"- {assessment['interpretation']}")
        if gold.get("per_task"):
            lines.extend(
                [
                    "",
                    "| Task | Accepts | Changes required | Findings | Agreement |",
                    "| --- | ---: | ---: | ---: | ---: |",
                ]
            )
            for item in gold["per_task"]:
                review = item["review"]
                lines.append(
                    f"| `{item['task_id']}` | {review['accept_count']} | "
                    f"{review['changes_required_count']} | {review['findings']} | "
                    f"{_display(review['verdict_agreement_rate'], percent=True)} |"
                )
            gold_findings = [
                (item["task_id"], attempt["attempt"], finding)
                for item in gold["per_task"]
                for attempt in item["review"]["attempts"]
                for finding in attempt["findings"]
            ]
            if gold_findings:
                lines.extend(["", "Gold finding evidence:", ""])
                for task_id, attempt, finding in gold_findings:
                    lines.append(
                        f"- `{task_id}` attempt {attempt}, {finding['severity']} "
                        f"{finding['category']} at `{finding['file']}:{finding['line']}`: "
                        f"{finding['evidence']} Required action: {finding['required_action']}"
                    )
    lines.extend(["", "## 5. Infrastructure failures", ""])
    failures = report["infrastructure_failures"]
    if failures:
        for failure in failures:
            lines.append(
                f"- `{failure['candidate_id']}` attempt {failure['attempt']}: "
                + "; ".join(failure["errors"])
            )
    else:
        lines.append("None recorded.")
    if report["warnings"]:
        lines.extend(["", "Warnings:", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    lines.extend(["", "## 6. Evidence limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report["evidence_limitations"])
    lines.append("")
    return "\n".join(lines)


def write_review_reports(run_directory: Path, report: dict[str, Any]) -> None:
    (run_directory / "review-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_directory / "review-report.md").write_text(
        render_review_markdown(report), encoding="utf-8"
    )


def regenerate_review_reports(run_directory: Path) -> dict[str, Any]:
    report = build_review_report(run_directory)
    write_review_reports(run_directory, report)
    return report
