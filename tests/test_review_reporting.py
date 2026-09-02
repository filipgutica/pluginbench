import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pluginbench.review_models import (
    ReviewAttemptResult,
    ReviewResponse,
    ReviewUsage,
    ReviewVerdict,
)
from pluginbench.review_reporting import (
    build_review_report,
    regenerate_review_reports,
    write_review_reports,
)


def _task_result(*, passed: bool) -> dict[str, object]:
    return {
        "task_id": "task-a",
        "attempts": 1,
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "duration_seconds": 10.0,
        "infrastructure_errors": [],
        "skill_calls": [],
        "usage": {
            "input_tokens": 100,
            "cached_tokens": 60,
            "output_tokens": 20,
            "cost_usd": 0.01,
        },
    }


def _write_artifacts(tmp_path: Path) -> tuple[Path, tuple[ReviewAttemptResult, ...]]:
    run = tmp_path / "review-run"
    (run / "private").mkdir(parents=True)
    labels = ("baseline", "full", "core", "gold")
    candidate_map = {
        f"candidate-{label}": {"task_id": "task-a", "source_label": label} for label in labels
    }
    (run / "private" / "candidate-map.json").write_text(
        json.dumps(candidate_map, indent=2, sort_keys=True) + "\n"
    )
    stats = {
        candidate_id: {"patch_files": ["src/a.py"], "lines_added": 2, "lines_removed": 1}
        for candidate_id in candidate_map
    }
    manifest = {
        "schema_version": 1,
        "kind": "blinded_reviewer_proxy",
        "study_name": "fixture-review",
        "started_at": "2026-08-31T10:00:00+00:00",
        "finished_at": "2026-08-31T10:10:00+00:00",
        "reviewer": {"model": "gpt-5.6-luna", "attempts": 2},
        "expected_attempts_per_candidate": 2,
        "compatibility_fingerprint": "sha256:" + "a" * 64,
        "dataset": {"name": "SWE-bench", "version": "revision"},
        "source_arms": {"baseline": "b", "full": "f", "core": "c"},
        "source_arm_provenance": {
            "baseline": {"skill_digest": None},
            "full": {"skill_digest": "sha256:full"},
            "core": {"skill_digest": "sha256:core"},
        },
        "review_tooling": {"promptfoo_version": "0.122.2", "container_image": "image"},
        "tasks": [
            {
                "task_id": "task-a",
                "digest": "sha256:" + "b" * 64,
                "repo": "org/repo",
                "base_commit": "c" * 40,
                "implementation_results": {
                    "baseline": _task_result(passed=False),
                    "full": _task_result(passed=False),
                    "core": _task_result(passed=True),
                },
            }
        ],
        "excluded_tasks": [{"task_id": "task-x", "reason": "not scoreable"}],
        "candidates": stats,
        "calibration_enabled": True,
        "limitations": ["Reviewer proxy only."],
    }
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    attempts: list[ReviewAttemptResult] = []
    for label in labels:
        for attempt_number in (1, 2):
            if label == "full":
                response = ReviewResponse.model_validate(
                    {
                        "verdict": "changes_required",
                        "findings": [
                            {
                                "category": "correctness",
                                "severity": "major",
                                "file": "src/a.py",
                                "line": 4,
                                "evidence": "The value is discarded.",
                                "required_action": "Return the value.",
                                "confidence": 0.9,
                            }
                        ],
                    }
                )
            elif label == "core" and attempt_number == 2:
                response = ReviewResponse.model_validate(
                    {
                        "verdict": "changes_required",
                        "findings": [
                            {
                                "category": "testing",
                                "severity": "minor",
                                "file": "tests/test_a.py",
                                "line": 2,
                                "evidence": "The regression case is uncovered.",
                                "required_action": "Add the regression case.",
                                "confidence": 0.7,
                            }
                        ],
                    }
                )
            else:
                response = ReviewResponse(verdict=ReviewVerdict.ACCEPT, findings=[])
            result = ReviewAttemptResult(
                candidate_id=f"candidate-{label}",
                attempt=attempt_number,
                response=response,
                usage=ReviewUsage(
                    input_tokens=10,
                    cached_tokens=None if label == "core" else 2,
                    uncached_input_tokens=None if label == "core" else 8,
                    output_tokens=3,
                    cost_usd=0.001,
                ),
                duration_seconds=1.0,
            )
            attempts.append(result)
            directory = run / "reviews" / result.candidate_id / f"attempt-{attempt_number:02d}"
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
    return run, tuple(attempts)


def test_review_report_pairs_tasks_and_keeps_usage_missing(tmp_path: Path) -> None:
    run, attempts = _write_artifacts(tmp_path)

    report = build_review_report(run, attempts=attempts)

    assert report["capability_results"]["core"]["tasks_passed"] == 1
    assert report["reviewer_proxy_results"]["full"]["changes_required_rate"] == 1.0
    assert report["reviewer_proxy_results"]["full"]["findings_by_category"] == {"correctness": 2}
    assert report["reviewer_proxy_results"]["core"]["verdict_agreement_rate"] == 0.0
    assert report["reviewer_proxy_results"]["core"]["reviewer_usage"]["cached_tokens"] is None
    assert report["gold_patch_calibration"]["accept_rate"] == 1.0
    assert (
        report["paired_review_differences"][0]["mean_task_paired_changes_required_rate_difference"]
        == 1.0
    )
    assert report["per_task"][0]["signals"]["baseline"][
        "verifier_failure_without_compatibility_finding"
    ]


def test_report_regeneration_is_identical_and_labels_the_proxy(tmp_path: Path) -> None:
    run, attempts = _write_artifacts(tmp_path)
    report = build_review_report(run, attempts=attempts)
    write_review_reports(run, report)
    original_json = (run / "review-report.json").read_bytes()
    original_markdown = (run / "review-report.md").read_bytes()

    regenerate_review_reports(run)

    assert (run / "review-report.json").read_bytes() == original_json
    assert (run / "review-report.md").read_bytes() == original_markdown
    assert "automated reviewer-proxy results" in original_markdown.decode()
    assert "Action-required finding evidence" in original_markdown.decode()
    assert "the saved verifier failed" in original_markdown.decode()


def test_missing_attempt_produces_an_incomplete_data_warning(tmp_path: Path) -> None:
    run, _ = _write_artifacts(tmp_path)
    (run / "reviews" / "candidate-baseline" / "attempt-02" / "result.json").unlink()

    report = build_review_report(run)

    assert report["warnings"]
    assert report["reviewer_proxy_results"]["baseline"]["incomplete_candidates"] == [
        "candidate-baseline"
    ]
    comparisons = {item["comparison"]: item for item in report["paired_review_differences"]}
    full_minus_baseline = comparisons["full-minus-baseline"]
    assert full_minus_baseline["tasks"][0]["changes_required_rate_difference"] is None
    assert full_minus_baseline["mean_task_paired_changes_required_rate_difference"] is None
    assert (
        report["per_task"][0]["signals"]["baseline"][
            "verifier_failure_without_compatibility_finding"
        ]
        is None
    )
    assessment = report["gold_patch_calibration"]["calibration_assessment"]
    assert assessment["status"] == "insufficient_data"
    assert "Incomplete review samples" in assessment["interpretation"]


def test_duplicate_attempt_identity_is_rejected(tmp_path: Path) -> None:
    run, _ = _write_artifacts(tmp_path)
    path = run / "reviews/candidate-baseline/attempt-02/result.json"
    payload = json.loads(path.read_text())
    payload["attempt"] = 1
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="does not match its artifact path"):
        build_review_report(run)


def test_unknown_candidate_attempt_is_rejected(tmp_path: Path) -> None:
    run, attempts = _write_artifacts(tmp_path)
    tampered = attempts[0].model_copy(update={"candidate_id": "unknown-candidate"})

    with pytest.raises(ValueError, match="unknown candidate"):
        build_review_report(run, attempts=(tampered, *attempts[1:]))


def test_duplicate_task_arm_mapping_is_rejected(tmp_path: Path) -> None:
    run, attempts = _write_artifacts(tmp_path)
    candidate_map_path = run / "private/candidate-map.json"
    candidate_map = json.loads(candidate_map_path.read_text())
    candidate_map["candidate-duplicate"] = {
        "task_id": "task-a",
        "source_label": "baseline",
    }
    candidate_map_path.write_text(json.dumps(candidate_map))
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["candidates"]["candidate-duplicate"] = manifest["candidates"]["candidate-baseline"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="duplicate candidate mapping"):
        build_review_report(run, attempts=attempts)


def test_report_loads_after_the_fact_gold_calibration(tmp_path: Path) -> None:
    run, _ = _write_artifacts(tmp_path)
    candidate_map_path = run / "private/candidate-map.json"
    candidate_map = json.loads(candidate_map_path.read_text())
    gold_mapping = {"candidate-gold": candidate_map.pop("candidate-gold")}
    candidate_map_path.write_text(json.dumps(candidate_map, indent=2, sort_keys=True) + "\n")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    gold_stats = {"candidate-gold": manifest["candidates"].pop("candidate-gold")}
    manifest["calibration_enabled"] = False
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    calibration = run / "gold-calibration"
    (calibration / "private").mkdir(parents=True)
    (calibration / "private/candidate-map.json").write_text(
        json.dumps(gold_mapping, indent=2, sort_keys=True) + "\n"
    )
    shutil.move(run / "reviews/candidate-gold", calibration / "reviews/candidate-gold")
    calibration_manifest = {
        **manifest,
        "kind": "gold_patch_calibration",
        "candidates": gold_stats,
        "calibration_enabled": True,
        "source_review_manifest_digest": "sha256:"
        + hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    (calibration / "manifest.json").write_text(
        json.dumps(calibration_manifest, indent=2, sort_keys=True) + "\n"
    )

    report = build_review_report(run)

    gold = report["gold_patch_calibration"]
    assert gold["candidates"] == 1
    assert gold["accept_rate"] == 1.0
    assert gold["per_task"][0]["task_id"] == "task-a"
    assert gold["per_task"][0]["review"]["valid_reviews"] == 2

    calibration_manifest["expected_attempts_per_candidate"] = 1
    (calibration / "manifest.json").write_text(json.dumps(calibration_manifest))
    with pytest.raises(ValueError, match="attempt count does not match"):
        build_review_report(run)


def test_gold_calibration_warns_when_reference_patches_score_worse_than_all_arms(
    tmp_path: Path,
) -> None:
    run, attempts = _write_artifacts(tmp_path)
    changed_attempts: list[ReviewAttemptResult] = []
    for attempt in attempts:
        if attempt.candidate_id == "candidate-full" and attempt.attempt == 2:
            changed_attempts.append(
                attempt.model_copy(
                    update={"response": ReviewResponse(verdict=ReviewVerdict.ACCEPT, findings=[])}
                )
            )
            continue
        if attempt.candidate_id != "candidate-gold":
            changed_attempts.append(attempt)
            continue
        changed_attempts.append(
            attempt.model_copy(
                update={
                    "response": ReviewResponse.model_validate(
                        {
                            "verdict": "changes_required",
                            "findings": [
                                {
                                    "category": "correctness",
                                    "severity": "major",
                                    "file": "src/a.py",
                                    "line": 4,
                                    "evidence": "The reference patch has a reported problem.",
                                    "required_action": "Adjudicate the reported problem.",
                                    "confidence": 0.8,
                                },
                                {
                                    "category": "compatibility",
                                    "severity": "minor",
                                    "file": "src/a.py",
                                    "line": 5,
                                    "evidence": "The reviewer reports a compatibility risk.",
                                    "required_action": "Check the compatibility contract.",
                                    "confidence": 0.7,
                                },
                            ],
                        }
                    )
                }
            )
        )

    report = build_review_report(run, attempts=tuple(changed_attempts))

    assessment = report["gold_patch_calibration"]["calibration_assessment"]
    assert assessment["status"] == "warning"
    assert len(assessment["warnings"]) == 2
    assert "validated quality evidence" in assessment["interpretation"]
    assert assessment["warnings"] == report["warnings"]
